"""Safety decisions live here, not in the prompt."""

from decimal import Decimal


def affected_ids(action):
    return [action.ad_set_id] + (
        [action.destination_ad_set_id] if action.destination_ad_set_id is not None else []
    )


def check(action, snapshot):
    reasons = []
    sets = snapshot["ad_sets"]
    source = sets.get(action.ad_set_id)
    for aid in affected_ids(action):
        item = sets.get(aid)
        if item is None or item["client_id"] != snapshot["client_id"]:
            reasons.append(f"ownership: {aid} is not owned by this client")
            continue
        if item["status"] != "active":
            reasons.append(f"inactive: {aid} is not active")
        if not item["eligible"]:
            reasons.append(
                f"insufficient_data: {aid} needs current-day data and at least 5 of 7 days"
            )
    if source and not source["flags"]:
        reasons.append("unflagged: the source was not flagged by the detection rules")
    projected = {aid: item["daily_budget_inr"] for aid, item in sets.items()}
    if action.action_type == "budget_shift" and source:
        if action.ad_set_id == action.destination_ad_set_id:
            reasons.append("same_ad_set: source and destination must differ")
        limit = (
            Decimal(source["daily_budget_inr"]) * Decimal(snapshot["policy"]["max_shift_pct"]) / 100
        )
        if action.amount_inr > limit:
            reasons.append(f"max_shift: ₹{action.amount_inr} exceeds source limit ₹{limit}")
        if action.amount_inr > source["daily_budget_inr"]:
            reasons.append("negative_budget: source cannot become negative")
        dest = sets.get(action.destination_ad_set_id)
        if dest and not dest["healthy"]:
            reasons.append("unhealthy_destination: recipient must meet targets without fatigue")
        if dest and action.ad_set_id != action.destination_ad_set_id:
            projected[action.ad_set_id] -= action.amount_inr
            projected[action.destination_ad_set_id] += action.amount_inr
    if sum(projected.values()) > snapshot["policy"]["daily_budget_cap_inr"]:
        reasons.append("daily_cap: configured client budgets exceed the daily cap")
    return reasons


def stale_reasons(action, approved, current):
    reasons = []
    if approved["policy"] != current["policy"]:
        reasons.append("Client policy changed")
    for aid in affected_ids(action):
        before, after = approved["ad_sets"].get(aid), current["ad_sets"].get(aid)
        if before is None or after is None:
            reasons.append(f"Ad set ownership changed: {aid}")
            continue
        for field in ("daily_budget_inr", "status", "revision", "creative_id", "metrics"):
            if before[field] != after[field]:
                reasons.append(f"{aid}: {field} changed")
    return reasons
