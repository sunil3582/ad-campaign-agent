"""Daily checkpoints, human decisions, and the durable execution queue."""

import random
import time
from uuid import uuid4

from guardian.db import audit, jsonb
from guardian.guardrails import check, stale_reasons
from guardian.llm import Action, make_context, propose
from guardian.metrics import build_snapshot, ingest
from guardian.mock_ads_api import BlockedError, MockAdsAPI, RateLimitError, StaleError

OPEN = ("pending_approval", "approved", "executing", "retry_pending")


class Agent:
    def __init__(self, db, model, *, data_dir=None, api=None, sleep=time.sleep):
        self.db, self.model, self.data_dir = db, model, data_dir
        self.api = api or MockAdsAPI(db)
        self.sleep = sleep

    def ads_retry(self, operation):
        for attempt in range(3):
            try:
                return operation()
            except RateLimitError as exc:
                delay = max(exc.retry_after, 2**attempt)
                if attempt == 2 or delay > 5:
                    raise
                self.sleep(delay + random.uniform(0, 0.1))

    def snapshot(self, client_id, as_of):
        live = self.ads_retry(lambda: self.api.list_ad_sets(client_id))
        with self.db.connect() as conn:
            return build_snapshot(conn, client_id, as_of, live)

    def daily(self, as_of, client_id=None, *, execute=True):
        clients = self.db.rows(
            """SELECT client_id FROM clients WHERE (%s::text IS NULL OR client_id=%s)
               ORDER BY client_id""",
            (client_id, client_id),
        )
        if not clients:
            raise ValueError(f"Unknown client: {client_id}")
        results = []
        for client in clients:
            cid = client["client_id"]
            with self.db.lock(f"daily:{cid}:{as_of}") as acquired:
                if not acquired:
                    results.append({"client_id": cid, "status": "busy"})
                    continue
                try:
                    results.append(self.daily_client(cid, as_of))
                except Exception as exc:
                    self.db.execute(
                        "UPDATE runs SET error=%s WHERE client_id=%s AND date=%s",
                        (str(exc)[:1500], cid, as_of),
                    )
                    with self.db.connect() as conn:
                        audit(conn, "run_failed", client_id=cid, detail={"error": str(exc)[:1500]})
                    results.append({"client_id": cid, "status": "failed", "error": str(exc)[:1500]})
        executions = self.execute_approved(client_id=client_id) if execute else []
        return {"date": as_of.isoformat(), "clients": results, "executions": executions}

    def daily_client(self, client_id, as_of):
        with self.db.connect() as conn:
            run = conn.execute(
                """INSERT INTO runs(client_id,date) VALUES (%s,%s)
                   ON CONFLICT (client_id,date) DO UPDATE SET client_id=EXCLUDED.client_id
                   RETURNING *""",
                (client_id, as_of),
            ).fetchone()
        if run["phase"] == "completed":
            return {"client_id": client_id, "status": "already_completed", **run["summary"]}
        if run["phase"] != "ingested":
            count = ingest(self.db, client_id, as_of, self.data_dir)
            today = self.db.one(
                """SELECT count(*) AS n FROM daily_metrics m JOIN ad_sets a USING(ad_set_id)
                   WHERE a.client_id=%s AND m.date=%s""",
                (client_id, as_of),
            )["n"]
            self.db.execute(
                """UPDATE runs SET phase=%s,imported_rows=imported_rows+%s,error=NULL
                   WHERE id=%s""",
                ("ingested" if today else "no_data", count, run["id"]),
            )
            if not today:
                return {"client_id": client_id, "status": "no_data", "date": str(as_of)}
        snapshot = self.snapshot(client_id, as_of)
        return {
            "client_id": client_id,
            "status": "completed",
            **self.create_proposals(run["id"], snapshot, complete_run=True),
        }

    def create_proposals(
        self,
        run_id,
        snapshot,
        *,
        subjects=None,
        replaces=None,
        complete_run=False,
    ):
        client_id = snapshot["client_id"]
        with self.db.lock(f"generation:{client_id}") as acquired:
            if not acquired:
                raise ValueError("Client proposal generation is busy; retry this command")
            flagged = {
                aid for aid, a in snapshot["ad_sets"].items() if a["flags"] and a["eligible"]
            }
            outstanding = self.db.rows(
                "SELECT id,ad_set_id FROM proposals WHERE client_id=%s AND status=ANY(%s)",
                (client_id, list(OPEN)),
            )
            pending_ids = {r["ad_set_id"] for r in outstanding}
            candidates = (flagged if subjects is None else flagged & set(subjects)) - pending_ids
            summary = {
                "flagged": sorted(flagged),
                "existing_pending": sorted(pending_ids),
                "proposal_ids": [],
                "abstentions": [],
                "skipped": [
                    {
                        "ad_set_id": aid,
                        "status": ad["status"],
                        "observed_days": ad["metrics"]["observed_days"],
                        "reason": "inactive" if ad["status"] != "active" else "insufficient_data",
                    }
                    for aid, ad in snapshot["ad_sets"].items()
                    if not ad["eligible"]
                ],
            }
            result, call_id = (None, None)
            if candidates:
                context = make_context(self.db, snapshot, candidates)
                result, call_id = propose(self.db, run_id, context, self.model, sleep=self.sleep)
            decisions = []
            with self.db.connect() as conn:
                if result:
                    summary["abstentions"] = [a.model_dump() for a in result.abstentions]
                    for action in result.proposals:
                        reasons = check(action, snapshot)
                        pid, status = str(uuid4()), "blocked" if reasons else "pending_approval"
                        conn.execute(
                            """INSERT INTO proposals(id,client_id,ad_set_id,run_id,llm_call_id,
                               as_of,payload,snapshot,status,reasons,replaces_proposal_id)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (
                                pid,
                                client_id,
                                action.ad_set_id,
                                run_id,
                                call_id,
                                snapshot["as_of"],
                                jsonb(action.model_dump()),
                                jsonb(snapshot),
                                status,
                                jsonb(reasons),
                                replaces,
                            ),
                        )
                        audit(
                            conn,
                            status,
                            proposal_id=pid,
                            client_id=client_id,
                            detail={"reasons": reasons, "llm_call_id": call_id},
                        )
                        summary["proposal_ids"].append(pid)
                        decisions.append({"proposal_id": pid, "status": status, "reasons": reasons})
                    conn.execute(
                        "UPDATE llm_calls SET decision=%s WHERE id=%s",
                        (
                            jsonb({"proposals": decisions, "abstentions": summary["abstentions"]}),
                            call_id,
                        ),
                    )
                if complete_run:
                    # Proposals and the completed checkpoint commit together.
                    conn.execute(
                        """UPDATE runs SET phase='completed',summary=%s,error=NULL,finished_at=now()
                           WHERE id=%s""",
                        (jsonb(summary), run_id),
                    )
                    audit(conn, "run_completed", client_id=client_id, detail=summary)
            return summary

    def review(self, proposal_id, reviewer, *, rejection_reason=None):
        reviewer = reviewer.strip()
        if not reviewer:
            raise ValueError("Reviewer is required")
        if rejection_reason is not None and not rejection_reason.strip():
            raise ValueError("Rejection requires a nonempty reason")
        with self.db.connect() as conn:
            proposal = conn.execute(
                "SELECT * FROM proposals WHERE id=%s FOR UPDATE", (proposal_id,)
            ).fetchone()
            if proposal is None:
                raise ValueError("Proposal not found")
            if proposal["status"] != "pending_approval":
                return {"id": str(proposal["id"]), "status": proposal["status"], "changed": False}
            status = "rejected" if rejection_reason is not None else "approved"
            conn.execute(
                """UPDATE proposals SET status=%s,reviewer=%s,rejection_reason=%s,reviewed_at=now()
                   WHERE id=%s""",
                (
                    status,
                    reviewer,
                    rejection_reason.strip() if rejection_reason is not None else None,
                    proposal_id,
                ),
            )
            audit(
                conn,
                status,
                proposal_id=proposal_id,
                client_id=proposal["client_id"],
                actor=reviewer,
                detail={"reason": rejection_reason},
            )
        return {"id": str(proposal_id), "status": status, "changed": True}

    def latest_date(self, client_id, fallback):
        latest = self.db.one(
            """SELECT max(m.date) AS date FROM daily_metrics m JOIN ad_sets a USING(ad_set_id)
               WHERE a.client_id=%s""",
            (client_id,),
        )["date"]
        return max(latest or fallback, fallback)

    def refresh_stale(self, proposal_id):
        with self.db.lock(f"refresh:{proposal_id}") as acquired:
            if not acquired:
                return
            old = self.db.one("SELECT * FROM proposals WHERE id=%s", (proposal_id,))
            if old["status"] != "stale" or old["refresh_status"] != "pending":
                return
            try:
                existing = self.db.one(
                    "SELECT id FROM proposals WHERE replaces_proposal_id=%s", (proposal_id,)
                )
                if existing:
                    self.db.execute(
                        """UPDATE proposals SET refresh_status='completed',last_error=NULL
                           WHERE id=%s""",
                        (proposal_id,),
                    )
                    return
                snapshot = self.snapshot(
                    old["client_id"], self.latest_date(old["client_id"], old["as_of"])
                )
                source = snapshot["ad_sets"].get(old["ad_set_id"])
                if source and source["status"] == "active" and not source["eligible"]:
                    raise ValueError("Waiting for sufficient current-window data before refreshing")
                needed = bool(source and source["flags"] and source["eligible"])
                if needed:
                    self.create_proposals(
                        old["run_id"],
                        snapshot,
                        subjects=[old["ad_set_id"]],
                        replaces=str(old["id"]),
                    )
                self.db.execute(
                    "UPDATE proposals SET refresh_status=%s,last_error=NULL WHERE id=%s",
                    ("completed" if needed else "no_longer_needed", proposal_id),
                )
            except Exception as exc:
                self.db.execute(
                    "UPDATE proposals SET last_error=%s WHERE id=%s", (str(exc)[:1500], proposal_id)
                )

    def mark_stale(self, proposal, reason):
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE proposals SET status='stale',reasons=%s,refresh_status='pending'
                   WHERE id=%s""",
                (jsonb([reason]), proposal["id"]),
            )
            audit(
                conn,
                "stale",
                proposal_id=proposal["id"],
                client_id=proposal["client_id"],
                detail={"reason": reason},
            )
        self.refresh_stale(str(proposal["id"]))

    def execute_proposal(self, proposal_id, *, after_apply=None):
        with self.db.lock(f"execution:{proposal_id}") as acquired:
            if not acquired:
                return {"id": str(proposal_id), "status": "busy"}
            proposal = self.db.one("SELECT * FROM proposals WHERE id=%s", (proposal_id,))
            if proposal is None:
                raise ValueError("Proposal not found")
            if proposal["status"] not in ("approved", "executing", "retry_pending"):
                return {"id": str(proposal_id), "status": proposal["status"]}
            action = Action.model_validate(proposal["payload"])
            self.db.execute(
                "UPDATE proposals SET status='executing',attempt_count=attempt_count+1 WHERE id=%s",
                (proposal_id,),
            )
            try:
                receipt = self.api.get_operation(str(proposal_id), action, proposal["snapshot"])
                if receipt is None:
                    as_of = self.latest_date(proposal["client_id"], proposal["as_of"])
                    current = self.snapshot(proposal["client_id"], as_of)
                    stale = stale_reasons(action, proposal["snapshot"], current)
                    if stale:
                        raise StaleError("; ".join(stale))
                    blocked = check(action, current)
                    if blocked:
                        raise BlockedError("; ".join(blocked))
                    receipt = self.ads_retry(
                        lambda: self.api.apply_action(
                            str(proposal_id), action, proposal["snapshot"]
                        )
                    )
                if after_apply:
                    after_apply()  # Crash-test seam between platform commit and acknowledgement.
                with self.db.connect() as conn:
                    conn.execute(
                        """UPDATE proposals SET status='executed',receipt=%s,executed_at=now(),
                           next_attempt_at=NULL,last_error=NULL WHERE id=%s""",
                        (jsonb(receipt), proposal_id),
                    )
                    audit(
                        conn,
                        "executed",
                        proposal_id=proposal_id,
                        client_id=proposal["client_id"],
                        detail=receipt,
                    )
                status = "executed"
            except StaleError as exc:
                self.mark_stale(proposal, str(exc))
                status = "stale"
            except BlockedError as exc:
                self._execution_error(proposal, "blocked", str(exc))
                status = "blocked"
            except RateLimitError as exc:
                self._execution_error(
                    proposal, "retry_pending", str(exc), delay=max(1, exc.retry_after)
                )
                status = "retry_pending"
            except ValueError as exc:
                self._execution_error(proposal, "failed", str(exc))
                status = "failed"
            except Exception as exc:
                # An ambiguous transport failure is retried with the SAME key, never a fresh one.
                self._execution_error(proposal, "retry_pending", str(exc), delay=30)
                status = "retry_pending"
            return {"id": str(proposal_id), "status": status}

    def _execution_error(self, proposal, status, message, delay=None):
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE proposals SET status=%s,last_error=%s,reasons=%s,
                   next_attempt_at=CASE WHEN %s::integer IS NULL THEN NULL
                     ELSE now() + %s * interval '1 second' END WHERE id=%s""",
                (status, message[:1500], jsonb([message[:1500]]), delay, delay, proposal["id"]),
            )
            audit(
                conn,
                status,
                proposal_id=proposal["id"],
                client_id=proposal["client_id"],
                detail={"error": message[:1500]},
            )

    def execute_approved(self, *, client_id=None):
        results = []
        stale = self.db.rows(
            """SELECT id FROM proposals WHERE status='stale' AND refresh_status='pending'
               AND (%s::text IS NULL OR client_id=%s) ORDER BY created_at""",
            (client_id, client_id),
        )
        for row in stale:
            self.refresh_stale(str(row["id"]))
        queued = self.db.rows(
            """SELECT id FROM proposals WHERE status IN ('approved','executing','retry_pending')
               AND (next_attempt_at IS NULL OR next_attempt_at <= now())
               AND (%s::text IS NULL OR client_id=%s) ORDER BY created_at""",
            (client_id, client_id),
        )
        for row in queued:
            results.append(self.execute_proposal(str(row["id"])))
        return results
