"""Persistent mock platform with conditional writes and durable operation receipts.

The supplied JSON mock was not safe across processes or crashes. This small
Postgres-backed replacement keeps its methods and adds atomic apply_action.
The platform commits independently of the application's execution status.
"""

from datetime import date
from uuid import uuid4

from guardian.db import fingerprint, jsonb
from guardian.guardrails import check, stale_reasons
from guardian.metrics import build_snapshot


class RateLimitError(Exception):
    def __init__(self, retry_after=1):
        self.retry_after = retry_after
        super().__init__(f"429 Too Many Requests; retry after {retry_after}s")


class StaleError(Exception):
    pass


class BlockedError(Exception):
    pass


class MockAdsAPI:
    def __init__(self, db):
        self.db = db

    def configure_rate_limit(self, after=None, failures=None, retry_after=1):
        # failures=None means persistent 429; an integer makes it recoverable.
        if after is not None and after < 0 or failures is not None and failures < 0:
            raise ValueError("Rate-limit values cannot be negative")
        if retry_after < 0:
            raise ValueError("retry_after cannot be negative")
        self.db.execute(
            """UPDATE mock_controls SET calls=0,rate_limit_after=%s,remaining_failures=%s,
               retry_after=%s WHERE id=1""",
            (after, failures, retry_after),
        )

    def _tick(self):
        # Commit the counter even on a 429; otherwise retries would never recover.
        limited = False
        with self.db.connect() as conn:
            control = conn.execute(
                "UPDATE mock_controls SET calls=calls+1 WHERE id=1 RETURNING *"
            ).fetchone()
            after, remaining = control["rate_limit_after"], control["remaining_failures"]
            if after is not None and control["calls"] > after:
                limited = remaining is None or remaining > 0
                if limited and remaining is not None:
                    conn.execute("UPDATE mock_controls SET remaining_failures=remaining_failures-1")
        if limited:
            raise RateLimitError(control["retry_after"])

    def get_ad_set(self, ad_set_id):
        self._tick()
        state = self.db.one("SELECT * FROM mock_ad_sets WHERE ad_set_id=%s", (ad_set_id,))
        if state is None:
            raise ValueError(f"Unknown ad set: {ad_set_id}")
        return state

    def list_ad_sets(self, client_id):
        self._tick()
        return self.db.rows(
            "SELECT * FROM mock_ad_sets WHERE client_id=%s ORDER BY ad_set_id", (client_id,)
        )

    def get_operation(self, operation_key, action, snapshot):
        # Receipt lookup is not rate-limited, like the original mock's change_log.
        row = self.db.one("SELECT * FROM mock_operations WHERE operation_key=%s", (operation_key,))
        if row is None:
            return None
        self._check_hash(row, action, snapshot)
        return row["result"]

    @staticmethod
    def _check_hash(row, action, snapshot):
        if row["request_hash"] != fingerprint(
            {"action": action.model_dump(), "snapshot": snapshot}
        ):
            raise ValueError("Idempotency key already belongs to a different request")

    def apply_action(self, operation_key, action, snapshot, *, before_commit=None):
        receipt = self.get_operation(operation_key, action, snapshot)
        if receipt is not None:
            return receipt
        self._tick()
        client_id = snapshot["client_id"]
        with self.db.connect() as conn:
            # Every platform mutation, including manual edits, locks client first.
            conn.execute(
                "SELECT client_id FROM clients WHERE client_id=%s FOR UPDATE", (client_id,)
            )
            previous = conn.execute(
                "SELECT * FROM mock_operations WHERE operation_key=%s", (operation_key,)
            ).fetchone()
            if previous:
                self._check_hash(previous, action, snapshot)
                return previous["result"]
            live = conn.execute(
                "SELECT * FROM mock_ad_sets WHERE client_id=%s ORDER BY ad_set_id FOR UPDATE",
                (client_id,),
            ).fetchall()
            current = build_snapshot(conn, client_id, date.fromisoformat(snapshot["as_of"]), live)
            stale = stale_reasons(action, snapshot, current)
            if stale:
                raise StaleError("; ".join(stale))
            blocked = check(action, current)
            if blocked:
                raise BlockedError("; ".join(blocked))
            changes = []
            if action.action_type == "budget_shift":
                for aid, delta in (
                    (action.ad_set_id, -action.amount_inr),
                    (action.destination_ad_set_id, action.amount_inr),
                ):
                    before = current["ad_sets"][aid]["daily_budget_inr"]
                    conn.execute(
                        """UPDATE mock_ad_sets SET daily_budget_inr=daily_budget_inr+%s,
                           revision=revision+1 WHERE ad_set_id=%s""",
                        (delta, aid),
                    )
                    changes.append(
                        {"ad_set_id": aid, "old_budget": before, "new_budget": before + delta}
                    )
            elif action.action_type == "pause":
                conn.execute(
                    """UPDATE mock_ad_sets SET status='paused',revision=revision+1
                       WHERE ad_set_id=%s""",
                    (action.ad_set_id,),
                )
                changes.append(
                    {"ad_set_id": action.ad_set_id, "old_status": "active", "new_status": "paused"}
                )
            else:
                # A refresh is a simulated work item, not a deployed creative.
                changes.append(
                    {
                        "ad_set_id": action.ad_set_id,
                        "creative_id": current["ad_sets"][action.ad_set_id]["creative_id"],
                        "refresh_request_id": operation_key,
                        "status": "queued",
                        "brief": action.creative_brief,
                    }
                )
            result = {
                "operation_key": operation_key,
                "client_id": client_id,
                "action_type": action.action_type,
                "changes": changes,
            }
            conn.execute(
                """INSERT INTO mock_operations(operation_key,client_id,request_hash,actor,result)
                   VALUES (%s,%s,%s,'agent',%s)""",
                (
                    operation_key,
                    client_id,
                    fingerprint({"action": action.model_dump(), "snapshot": snapshot}),
                    jsonb(result),
                ),
            )
            if before_commit:
                before_commit()  # Test seam for rollback/crash injection, never an LLM tool.
        return result

    def set_daily_budget(self, ad_set_id, amount_inr, actor="marketer_manual"):
        if type(amount_inr) is not int or amount_inr < 0:
            raise ValueError("Budget must be nonnegative whole INR")
        return self._manual(ad_set_id, budget=amount_inr, actor=actor)

    def set_status(self, ad_set_id, status, actor="marketer_manual"):
        if status not in ("active", "paused"):
            raise ValueError("Status must be active or paused")
        return self._manual(ad_set_id, status=status, actor=actor)

    def _manual(self, ad_set_id, *, budget=None, status=None, actor):
        self._tick()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM mock_ad_sets WHERE ad_set_id=%s", (ad_set_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown ad set: {ad_set_id}")
            conn.execute(
                "SELECT client_id FROM clients WHERE client_id=%s FOR UPDATE", (row["client_id"],)
            )
            before = conn.execute(
                "SELECT * FROM mock_ad_sets WHERE ad_set_id=%s FOR UPDATE", (ad_set_id,)
            ).fetchone()
            after = conn.execute(
                """UPDATE mock_ad_sets SET daily_budget_inr=COALESCE(%s,daily_budget_inr),
                   status=COALESCE(%s,status),revision=revision+1 WHERE ad_set_id=%s RETURNING *""",
                (budget, status, ad_set_id),
            ).fetchone()
            key = f"manual:{uuid4()}"
            change = {
                "ad_set_id": ad_set_id,
                "old_budget": before["daily_budget_inr"],
                "new_budget": after["daily_budget_inr"],
                "old_status": before["status"],
                "new_status": after["status"],
            }
            result = {"operation_key": key, "changes": [change]}
            conn.execute(
                """INSERT INTO mock_operations(operation_key,client_id,request_hash,actor,result)
                   VALUES (%s,%s,%s,%s,%s)""",
                (key, row["client_id"], fingerprint(change), actor, jsonb(result)),
            )
        return after

    def change_log(self, ad_set_id):
        entries = self.db.rows(
            "SELECT operation_key,actor,result,created_at FROM mock_operations ORDER BY created_at"
        )
        return [
            {
                "operation_key": r["operation_key"],
                "actor": r["actor"],
                "at": r["created_at"],
                **change,
            }
            for r in entries
            for change in r["result"]["changes"]
            if change["ad_set_id"] == ad_set_id
        ]
