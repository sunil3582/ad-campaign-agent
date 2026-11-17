import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier
from uuid import uuid4

import pytest

from guardian.cli import export_email, main
from guardian.db import canonical
from guardian.llm import Action
from guardian.mock_ads_api import MockAdsAPI
from guardian.workflow import Agent
from tests.fakes import FakeModel

DAY = date(2026, 11, 9)


def agent_changes(api, ad_id):
    return [entry for entry in api.change_log(ad_id) if entry["actor"] == "agent"]


def test_unapproved_cannot_execute(agent, proposal):
    """Input: pending proposal sent to executor. Expected: no platform mutation."""
    result = agent.execute_proposal(str(proposal["id"]))
    assert result["status"] == "pending_approval"
    assert agent_changes(agent.api, "AS-102") == []


def test_double_approval_executes_once(agent, proposal):
    """Input: duplicate approvals/executions. Expected: one effect per ad set."""
    pid = str(proposal["id"])
    assert agent.review(pid, "marketer")["changed"]
    assert not agent.review(pid, "marketer")["changed"]
    assert agent.execute_proposal(pid)["status"] == "executed"
    assert agent.execute_proposal(pid)["status"] == "executed"
    assert len(agent_changes(agent.api, "AS-102")) == 1
    assert len(agent_changes(agent.api, "AS-101")) == 1


def test_simultaneous_approval_executes_once(agent, proposal, db):
    """Input: concurrent reviewers on separate DB connections. Expected: one effect."""
    barrier = Barrier(2)
    pid = str(proposal["id"])

    def worker():
        other = Agent(db, FakeModel(), sleep=lambda _: None)
        barrier.wait(timeout=5)
        decision = other.review(pid, "concurrent reviewer")
        other.execute_proposal(pid)
        return decision

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(lambda _: worker(), range(2)))
    agent.execute_proposal(pid)
    assert sum(d["changed"] for d in decisions) == 1
    assert len(agent_changes(agent.api, "AS-102")) == 1
    assert len(agent_changes(agent.api, "AS-101")) == 1


def test_mock_itself_deduplicates_concurrent_calls(agent, proposal, db):
    """Input: concurrent keyed API calls bypassing executor lock. Expected: one durable receipt."""
    action = Action.model_validate(proposal["payload"])
    key, barrier = str(proposal["id"]), Barrier(2)

    def worker():
        api = MockAdsAPI(db)
        barrier.wait(timeout=5)
        return api.apply_action(key, action, proposal["snapshot"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: worker(), range(2)))
    assert receipts[0] == receipts[1]
    assert len(agent_changes(agent.api, "AS-102")) == 1


@pytest.mark.parametrize("ad_id", ["AS-102", "AS-101"])
def test_manual_change_makes_approval_stale(agent, proposal, db, ad_id):
    """Input: source/destination manually changed. Expected: stale; replacement needs approval."""
    pid = str(proposal["id"])
    agent.api.set_daily_budget(ad_id, 9000)
    agent.review(pid, "marketer")
    assert agent.execute_proposal(pid)["status"] == "stale"
    assert agent.api.get_ad_set(ad_id)["daily_budget_inr"] == 9000
    assert not agent_changes(agent.api, ad_id)
    fresh = db.one("SELECT * FROM proposals WHERE replaces_proposal_id=%s", (pid,))
    assert fresh["status"] == "pending_approval"
    assert fresh["reviewer"] is None
    assert fresh["snapshot"]["ad_sets"][ad_id]["daily_budget_inr"] == 9000


def test_change_away_and_back_is_stale(agent, proposal, db):
    """Input: budget changed then restored. Expected: revision detects stale approval."""
    pid = str(proposal["id"])
    agent.api.set_daily_budget("AS-102", 9000)
    agent.api.set_daily_budget("AS-102", 15000)
    agent.review(pid, "marketer")
    assert agent.execute_proposal(pid)["status"] == "stale"
    assert agent.api.get_ad_set("AS-102")["revision"] == 3
    assert not agent_changes(agent.api, "AS-102")


def test_manual_change_between_read_and_write(agent, proposal, monkeypatch):
    """Input: manual edit after GET. Expected: atomic write rejects it and preserves edit."""
    original = agent.api.apply_action

    def edit_then_apply(*args, **kwargs):
        agent.api.set_daily_budget("AS-102", 8700)
        return original(*args, **kwargs)

    monkeypatch.setattr(agent.api, "apply_action", edit_then_apply)
    pid = str(proposal["id"])
    agent.review(pid, "marketer")
    assert agent.execute_proposal(pid)["status"] == "stale"
    assert agent.api.get_ad_set("AS-102")["daily_budget_inr"] == 8700
    assert not agent_changes(agent.api, "AS-102")


def test_crash_after_platform_commit_is_reconciled(agent, proposal, db):
    """Input: crash after commit, then 429. Expected: receipt avoids another write."""

    class ProcessCrash(BaseException):
        pass

    def crash():
        raise ProcessCrash()

    pid = str(proposal["id"])
    agent.review(pid, "marketer")
    with pytest.raises(ProcessCrash):
        agent.execute_proposal(pid, after_apply=crash)
    assert db.one("SELECT status FROM proposals WHERE id=%s", (pid,))["status"] == "executing"
    agent.api.configure_rate_limit(after=0)
    restarted = Agent(db, FakeModel(), sleep=lambda _: None)
    assert restarted.execute_proposal(pid)["status"] == "executed"
    assert len(agent_changes(restarted.api, "AS-102")) == 1


def test_transfer_failure_rolls_back_both_legs(agent, proposal):
    """Input: failure inside transfer transaction. Expected: no partial change or receipt."""
    action = Action.model_validate(proposal["payload"])
    key = str(uuid4())

    def fail():
        raise RuntimeError("Injected failure before commit")

    with pytest.raises(RuntimeError):
        agent.api.apply_action(key, action, proposal["snapshot"], before_commit=fail)
    assert agent.api.get_ad_set("AS-102")["daily_budget_inr"] == 15000
    assert agent.api.get_ad_set("AS-101")["daily_budget_inr"] == 15000
    assert agent.api.get_operation(key, action, proposal["snapshot"]) is None
    assert agent_changes(agent.api, "AS-102") == []


def test_same_key_different_payload_refused(agent, proposal):
    """Input: successful key reused with a changed amount. Expected: idempotency conflict."""
    action = Action.model_validate(proposal["payload"])
    key = str(proposal["id"])
    agent.api.apply_action(key, action, proposal["snapshot"])
    changed = action.model_copy(update={"amount_inr": action.amount_inr + 1})
    with pytest.raises(ValueError, match="different request"):
        agent.api.apply_action(key, changed, proposal["snapshot"])
    assert len(agent_changes(agent.api, "AS-102")) == 1


def test_permanent_rate_limit_queues_bounded_retry(agent, proposal, db):
    """Input: every ads call returns 429. Expected: three attempts then durable retry_pending."""
    pid = str(proposal["id"])
    agent.review(pid, "marketer")
    agent.api.configure_rate_limit(after=0, retry_after=0)
    assert agent.execute_proposal(pid)["status"] == "retry_pending"
    assert db.one("SELECT calls FROM mock_controls")["calls"] == 3
    saved = db.one("SELECT * FROM proposals WHERE id=%s", (pid,))
    assert saved["next_attempt_at"] is not None
    assert not agent_changes(agent.api, "AS-102")


def test_recoverable_rate_limit_retries(agent, proposal):
    """Input: first two API calls return 429. Expected: execution succeeds once on recovery."""
    pid = str(proposal["id"])
    agent.review(pid, "marketer")
    agent.api.configure_rate_limit(after=0, failures=2, retry_after=0)
    assert agent.execute_proposal(pid)["status"] == "executed"
    assert len(agent_changes(agent.api, "AS-102")) == 1


def test_daily_resume_does_not_redo_finished_client(agent, db):
    """Input: C1 done, permanent 429 interrupts C2/C3. Expected: resume skips C1 model calls."""
    agent.api.configure_rate_limit(after=1, retry_after=0)
    first = agent.daily(DAY, execute=False)
    assert [c["status"] for c in first["clients"]] == ["completed", "failed", "failed"]
    calls_before = db.one("SELECT count(*) AS n FROM llm_calls WHERE client_id='C1'")["n"]
    agent.api.configure_rate_limit()
    second = agent.daily(DAY, execute=False)
    assert [c["status"] for c in second["clients"]] == [
        "already_completed",
        "completed",
        "completed",
    ]
    assert db.one("SELECT count(*) AS n FROM llm_calls WHERE client_id='C1'")["n"] == calls_before


def test_policy_change_replaces_approval(agent, proposal, db):
    """Input: cap lowered after proposal. Expected: old approval stale, replacement blocked."""
    db.execute(
        "UPDATE clients SET daily_budget_cap_inr=50000,revision=revision+1 WHERE client_id='C1'"
    )
    pid = str(proposal["id"])
    agent.review(pid, "marketer")
    assert agent.execute_proposal(pid)["status"] == "stale"
    replacement = db.one("SELECT * FROM proposals WHERE replaces_proposal_id=%s", (pid,))
    assert replacement["status"] == "blocked"
    assert not agent_changes(agent.api, "AS-102")


def test_unrelated_manual_edit_rechecks_client_cap(agent, proposal):
    """Input: unrelated budget pushes C1 over cap. Expected: approved shift blocked."""
    agent.api.set_daily_budget("AS-104", 90000)
    pid = str(proposal["id"])
    agent.review(pid, "marketer")
    assert agent.execute_proposal(pid)["status"] == "blocked"
    assert not agent_changes(agent.api, "AS-102")


def test_new_metrics_replace_old_evidence(agent, proposal, db):
    """Input: new day ingested before delayed execution. Expected: old evidence stale."""
    db.execute(
        """INSERT INTO daily_metrics SELECT ad_set_id,date+1,spend_inr,impressions,clicks,
           conversions,revenue_inr,frequency FROM daily_metrics WHERE date='2026-11-09'"""
    )
    pid = str(proposal["id"])
    agent.review(pid, "marketer")
    assert agent.execute_proposal(pid)["status"] == "stale"
    fresh = db.one("SELECT * FROM proposals WHERE replaces_proposal_id=%s", (pid,))
    assert fresh["as_of"] == date(2026, 11, 10)


def test_stale_refresh_failure_remains_queued(agent, proposal, db):
    """Input: stale approval plus unavailable LLM. Expected: no write; refresh remains pending."""

    class Unavailable:
        name = "unavailable-test"

        def generate(self, request):
            raise RuntimeError("Provider unavailable")

    agent.api.set_daily_budget("AS-102", 9000)
    pid = str(proposal["id"])
    agent.review(pid, "marketer")
    agent.model = Unavailable()
    assert agent.execute_proposal(pid)["status"] == "stale"
    old = db.one("SELECT * FROM proposals WHERE id=%s", (pid,))
    assert old["refresh_status"] == "pending" and old["last_error"]
    assert not agent_changes(agent.api, "AS-102")
    agent.model = FakeModel()
    agent.execute_approved()
    assert (
        db.one("SELECT count(*) AS n FROM proposals WHERE replaces_proposal_id=%s", (pid,))["n"]
        == 1
    )


def test_pause_and_refresh_execute_once(db):
    """Input: pause/refresh retried. Expected: one pause and one creative request."""

    class PauseSource(FakeModel):
        def generate(self, request):
            output = json.loads(super().generate(request))
            for action in output["proposals"]:
                if action["ad_set_id"] == "AS-102":
                    action.update(
                        action_type="pause",
                        amount_inr=0,
                        destination_ad_set_id=None,
                        amount_rationale=None,
                    )
            return canonical(output)

    agent = Agent(db, PauseSource(), sleep=lambda _: None)
    agent.daily(DAY, "C1", execute=False)
    for row in db.rows("SELECT * FROM proposals ORDER BY ad_set_id"):
        pid = str(row["id"])
        agent.review(pid, "marketer")
        assert agent.execute_proposal(pid)["status"] == "executed"
        assert agent.execute_proposal(pid)["status"] == "executed"
    assert agent.api.get_ad_set("AS-102")["status"] == "paused"
    refresh = agent_changes(agent.api, "AS-104")
    assert len(refresh) == 1 and refresh[0]["status"] == "queued"
    assert refresh[0]["creative_id"] == "CR-1004"


def test_approval_rejection_race_has_one_winner(agent, proposal):
    """Input: simultaneous approve and reject. Expected: one review decision wins."""
    pid, barrier = str(proposal["id"]), Barrier(2)

    def worker(reason):
        barrier.wait(timeout=5)
        return agent.review(pid, "reviewer", rejection_reason=reason)

    with ThreadPoolExecutor(max_workers=2) as pool:
        approve = pool.submit(worker, None)
        reject = pool.submit(worker, "Keep current allocation.")
        decisions = [approve.result(), reject.result()]
    assert sum(r["changed"] for r in decisions) == 1
    assert decisions[0]["status"] == decisions[1]["status"]


def test_queued_transfers_sharing_destination_are_revalidated(db):
    """Input: two approved shifts share AS-101. Expected: second is stale after first changes it."""

    class TwoShifts(FakeModel):
        def generate(self, request):
            output = json.loads(super().generate(request))
            for action in output["proposals"]:
                action.update(
                    action_type="budget_shift",
                    destination_ad_set_id="AS-101",
                    amount_inr=1000,
                    amount_rationale="Shift INR 1000 per day for this shared-destination test.",
                    creative_brief=None,
                )
            return canonical(output)

    agent = Agent(db, TwoShifts(), sleep=lambda _: None)
    agent.daily(DAY, "C1", execute=False)
    rows = db.rows("SELECT * FROM proposals ORDER BY ad_set_id")
    for row in rows:
        agent.review(str(row["id"]), "marketer")
    first, second = [str(r["id"]) for r in rows]
    assert agent.execute_proposal(first)["status"] == "executed"
    assert agent.execute_proposal(second)["status"] == "stale"
    assert len(agent_changes(agent.api, "AS-101")) == 1
    assert not agent_changes(agent.api, "AS-104")


def test_replacement_is_not_duplicated_after_refresh_crash(agent, proposal, db):
    """Input: replacement exists but old refresh checkpoint is pending. Expected: reuse it once."""
    pid = str(proposal["id"])
    agent.api.set_daily_budget("AS-102", 9000)
    agent.review(pid, "marketer")
    agent.execute_proposal(pid)
    calls = db.one("SELECT count(*) AS n FROM llm_calls")["n"]
    db.execute("UPDATE proposals SET refresh_status='pending' WHERE id=%s", (pid,))
    agent.refresh_stale(pid)
    assert (
        db.one("SELECT count(*) AS n FROM proposals WHERE replaces_proposal_id=%s", (pid,))["n"]
        == 1
    )
    assert db.one("SELECT count(*) AS n FROM llm_calls")["n"] == calls


def test_email_stdout_is_a_real_message(db, proposal, monkeypatch, capsys):
    """Input: CLI email export to stdout. Expected: .eml content for host file redirection."""
    monkeypatch.setenv("DATABASE_URL", db.url)
    assert main(["email-export", str(proposal["id"]), "--stdout"]) == 0
    text = capsys.readouterr().out
    assert text.startswith("Subject: Approval requested:")
    assert "guardian approve" in text


def test_email_is_local_and_grounded(db, proposal, tmp_path):
    """Input: email export. Expected: local draft with stored budgets and no send."""
    path = tmp_path / "review.eml"
    result = export_email(db, str(proposal["id"]), path)
    assert result["sent"] is False
    body = path.read_text()
    assert "15000 -> 13500" in body
    assert "Amount rationale: Shift INR 1500" in body
    assert "guardian approve" in body


def test_cli_gemini_and_review_commands(db, monkeypatch, capsys):
    """Input: CLI with fake Gemini transport. Expected: review and execution succeed."""
    monkeypatch.setenv("DATABASE_URL", db.url)
    monkeypatch.setenv("GEMINI_API_KEY", "test-only")
    monkeypatch.setattr("guardian.cli.GeminiModel", FakeModel)
    assert main(["run-daily", "--date", "2026-11-09", "--client", "C1"]) == 0
    assert main(["proposals", "list", "--client", "C1"]) == 0
    proposal = db.one("SELECT id FROM proposals WHERE ad_set_id='AS-102'")
    pid = str(proposal["id"])
    assert main(["proposals", "show", pid]) == 0
    assert main(["llm-calls", "--proposal", pid]) == 0
    assert main(["approve", pid, "--reviewer", "cli-reviewer"]) == 0
    assert main(["execute-approved"]) == 0
    assert db.one("SELECT status FROM proposals WHERE id=%s", (pid,))["status"] == "executed"
    assert "executed" in capsys.readouterr().out


def test_cli_llm_calls_marks_scripted_provider(db, monkeypatch, capsys):
    """Input: a fake provider response. Expected: CLI reports the actual provider name."""
    monkeypatch.setenv("DATABASE_URL", db.url)
    monkeypatch.setenv("GEMINI_API_KEY", "test-only")
    monkeypatch.setattr("guardian.cli.GeminiModel", FakeModel)
    assert main(["run-daily", "--date", "2026-11-09", "--client", "C1"]) == 0
    assert main(["llm-calls", "--client", "C1"]) == 0
    assert "test-fixture" in capsys.readouterr().out


def test_daily_requires_gemini_key(db, monkeypatch):
    """Input: no Gemini key. Expected: clear error, no proposals or fallback provider."""
    from guardian.cli import dispatch, parser

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="Set GEMINI_API_KEY"):
        dispatch(parser().parse_args(["run-daily", "--date", "2026-11-09"]), db)
    assert db.one("SELECT count(*) AS n FROM proposals")["n"] == 0
    assert db.one("SELECT count(*) AS n FROM llm_calls")["n"] == 0
