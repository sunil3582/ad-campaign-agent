"""The only user interface: explicit commands, JSON output, useful exit codes."""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from dotenv import load_dotenv

from guardian.db import Database
from guardian.llm import GeminiModel
from guardian.metrics import seed
from guardian.mock_ads_api import MockAdsAPI
from guardian.workflow import Agent


def parser():
    root = argparse.ArgumentParser(description="Human-approved Ad Budget Guardian")
    sub = root.add_subparsers(dest="command", required=True)
    daily = sub.add_parser("run-daily", help="Import, detect, propose, then drain approved work")
    daily.add_argument("--date", type=date.fromisoformat)
    daily.add_argument("--client")
    proposals = sub.add_parser("proposals", help="List or inspect saved recommendations")
    proposals.add_argument("operation", choices=["list", "show"])
    proposals.add_argument("id", nargs="?")
    proposals.add_argument("--client")
    proposals.add_argument("--status")
    llm_calls = sub.add_parser("llm-calls", help="Inspect logged Gemini calls")
    llm_calls.add_argument("--client")
    llm_calls.add_argument("--proposal", help="Only show the call that created this proposal")
    llm_calls.add_argument("--raw", action="store_true", help="Include the provider's raw response")
    for name in ("approve", "reject"):
        review = sub.add_parser(name)
        review.add_argument("id")
        review.add_argument("--reviewer", required=True)
        if name == "reject":
            review.add_argument("--reason", required=True)
    execute = sub.add_parser("execute-approved", help="Resume due approved/retry work")
    execute.add_argument("--client")
    mock = sub.add_parser("mock", help="Inspect/edit the simulated platform or inject 429s")
    commands = mock.add_subparsers(dest="mock_command", required=True)
    commands.add_parser("show")
    change = commands.add_parser("set")
    change.add_argument("ad_set_id")
    change.add_argument("budget", type=int)
    status = commands.add_parser("set-status")
    status.add_argument("ad_set_id")
    status.add_argument("status", choices=["active", "paused"])
    changes = commands.add_parser("changes")
    changes.add_argument("ad_set_id")
    rate = commands.add_parser("rate-limit")
    rate.add_argument("--after", type=int, help="Clear rate limiting when omitted")
    rate.add_argument("--failures", type=int, help="Omit for persistent errors")
    rate.add_argument("--retry-after", type=int, default=1)
    export = sub.add_parser("email-export", help="Write a local approval email; never send it")
    export.add_argument("id")
    export.add_argument("--output", type=Path)
    export.add_argument(
        "--stdout", action="store_true", help="Print .eml content for host redirection"
    )
    sub.add_parser("runs", help="Show persisted daily checkpoints")
    return root


def export_email(db, proposal_id, output=None, *, stdout=False):
    proposal = db.one("SELECT * FROM proposals WHERE id=%s", (proposal_id,))
    if proposal is None:
        raise ValueError("Proposal not found")
    if proposal["status"] != "pending_approval":
        raise ValueError("Approval emails can only be exported for pending proposals")
    action, snapshot = proposal["payload"], proposal["snapshot"]
    source = snapshot["ad_sets"][action["ad_set_id"]]
    lines = [
        f"Client: {proposal['client_id']}",
        f"Proposal: {proposal_id}",
        f"Status: {proposal['status']}",
        f"As of: {proposal['as_of']}",
        f"Action: {action['action_type']}",
        f"Subject: {action['ad_set_id']}",
        f"Reason: {action['reason']}",
    ]
    if action["action_type"] == "budget_shift":
        dest = snapshot["ad_sets"][action["destination_ad_set_id"]]
        amount = action["amount_inr"]
        lines.extend(
            [
                f"Shift: INR {amount}/day to {action['destination_ad_set_id']}",
                f"Source: INR {source['daily_budget_inr']} -> "
                f"{source['daily_budget_inr'] - amount}",
                f"Destination: INR {dest['daily_budget_inr']} -> "
                f"{dest['daily_budget_inr'] + amount}",
            ]
        )
        if action.get("amount_rationale"):
            lines.append(f"Amount rationale: {action['amount_rationale']}")
    lines.extend(
        [
            f"Evidence: {json.dumps(action['evidence'])}",
            f"Guardrail policy: {json.dumps(snapshot['policy'])}",
            f"Inspect: guardian proposals show {proposal_id}",
            f"Approve: guardian approve {proposal_id} --reviewer <name>",
            f"Reject: guardian reject {proposal_id} --reviewer <name> --reason '<reason>'",
            "Approval authorizes only the saved proposal. Execution rechecks current state.",
            "This is a local draft. Replies do not approve actions.",
        ]
    )
    message = EmailMessage()
    message["Subject"] = f"Approval requested: {proposal['client_id']} / {proposal_id}"
    message.set_content("\n".join(lines))
    if stdout:
        return message.as_string()
    output = output or Path("exports") / f"{proposal_id}.eml"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(message.as_bytes())
    return {"file": str(output.resolve()), "sent": False}


def dispatch(args, db):
    model = GeminiModel()
    agent = Agent(db, model)
    if args.command == "run-daily":
        if not os.environ.get("GEMINI_API_KEY"):
            raise ValueError("Set GEMINI_API_KEY in .env before running Gemini")
        yesterday = datetime.now(
            ZoneInfo(os.environ.get("BUSINESS_TIMEZONE", "Asia/Kolkata"))
        ).date()
        return agent.daily(args.date or yesterday - timedelta(days=1), args.client)
    if args.command == "proposals":
        if args.operation == "show":
            if not args.id:
                raise ValueError("proposals show requires an ID")
            result = db.one(
                """SELECT p.*,a.name AS ad_set_name FROM proposals p
                   JOIN ad_sets a USING(ad_set_id) WHERE p.id=%s""",
                (args.id,),
            )
            if result is None:
                raise ValueError("Proposal not found")
            return result
        return db.rows(
            """SELECT id,client_id,ad_set_id,payload->>'action_type' AS action,status,reasons,
                      as_of,last_error,refresh_status FROM proposals
               WHERE (%s::text IS NULL OR client_id=%s) AND (%s::text IS NULL OR status=%s)
               ORDER BY created_at""",
            (args.client, args.client, args.status, args.status),
        )
    if args.command == "llm-calls":
        raw_column = ",l.raw_output" if args.raw else ""
        return db.rows(
            f"""SELECT DISTINCT l.id,l.client_id,l.prompt_version,l.model,l.attempt,l.latency_ms,
                       l.decision,l.created_at{raw_column}
                FROM llm_calls l
                LEFT JOIN proposals p ON p.llm_call_id=l.id
                WHERE (%s::text IS NULL OR l.client_id=%s)
                  AND (%s::uuid IS NULL OR p.id=%s::uuid)
                ORDER BY l.created_at""",
            (args.client, args.client, args.proposal, args.proposal),
        )
    if args.command in ("approve", "reject"):
        return agent.review(args.id, args.reviewer, rejection_reason=getattr(args, "reason", None))
    if args.command == "execute-approved":
        return agent.execute_approved(client_id=args.client)
    if args.command == "runs":
        return db.rows("SELECT * FROM runs ORDER BY date,client_id")
    if args.command == "email-export":
        return export_email(db, args.id, args.output, stdout=args.stdout)
    api = MockAdsAPI(db)
    if args.mock_command == "show":
        return db.rows("SELECT * FROM mock_ad_sets ORDER BY ad_set_id")
    if args.mock_command == "set":
        return api.set_daily_budget(args.ad_set_id, args.budget)
    if args.mock_command == "set-status":
        return api.set_status(args.ad_set_id, args.status)
    if args.mock_command == "changes":
        return api.change_log(args.ad_set_id)
    api.configure_rate_limit(args.after, args.failures, args.retry_after)
    return db.one("SELECT * FROM mock_controls")


def main(argv=None):
    load_dotenv()
    args = parser().parse_args(argv)
    try:
        db = Database()
        db.migrate()
        seed(db)
        result = dispatch(args, db)
        if args.command == "email-export" and args.stdout:
            print(result, end="")
        else:
            print(json.dumps(result, indent=2, default=str, allow_nan=False))
        failed = {"failed", "retry_pending", "busy", "no_data"}
        if args.command == "run-daily":
            return int(any(r["status"] in failed for r in result["clients"] + result["executions"]))
        if args.command == "execute-approved":
            return int(any(r["status"] in failed for r in result))
        return 0
    except psycopg.Error as exc:
        # Never print a connection string or its credentials.
        print(
            f"Database error ({type(exc).__name__}). Check Postgres and DATABASE_URL.",
            file=sys.stderr,
        )
        return 1
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
