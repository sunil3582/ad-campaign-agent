from guardian.db import canonical


class FakeModel:
    """Deterministic model responses used only by tests."""

    name = "test-fixture"

    def generate(self, request):
        context = request["context"]
        actions = []
        for ad_id in context["flagged_ad_set_ids"]:
            source = context["snapshot"]["ad_sets"][ad_id]
            rejected_shift = any(
                r["ad_set_id"] == ad_id and r["action_type"] == "budget_shift"
                for r in context["rejections"]
            )
            destinations = context["healthy_destination_ids"]
            refresh = "fatigue" in source["flags"] or rejected_shift
            action = "creative_refresh" if refresh else "budget_shift" if destinations else "pause"
            actions.append(
                {
                    "action_type": action,
                    "ad_set_id": ad_id,
                    "destination_ad_set_id": destinations[0] if action == "budget_shift" else None,
                    "amount_inr": max(1, source["daily_budget_inr"] // 10)
                    if action == "budget_shift"
                    else 0,
                    "reason": "Test fixture: refresh after feedback or fatigue."
                    if refresh
                    else "Test fixture: reduce exposure to the flagged source.",
                    "amount_rationale": (
                        f"Shift INR {max(1, source['daily_budget_inr'] // 10)} per day, "
                        "one tenth of the source budget, to limit the size of this test."
                        if action == "budget_shift"
                        else None
                    ),
                    "evidence": source["metrics"],
                    "creative_brief": "Test a new hook and image; keep the offer consistent."
                    if refresh
                    else None,
                }
            )
        return canonical({"proposals": actions, "abstentions": []})
