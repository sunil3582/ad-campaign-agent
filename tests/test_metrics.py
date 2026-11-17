import csv
from datetime import date, timedelta
from decimal import Decimal

import pytest

from guardian.db import ROOT
from guardian.metrics import calculate_window, ingest, seed

DAY = date(2026, 11, 9)
POLICY = {"target_cpa_inr": "800", "target_roas": "3"}


def synthetic_rows(*, spend="1000", conversions=1, revenue="1000", impressions=10000):
    return [
        {
            "date": DAY - timedelta(days=i),
            "spend_inr": Decimal(spend),
            "conversions": conversions,
            "revenue_inr": Decimal(revenue),
            "impressions": impressions,
            "clicks": 100 if impressions else 0,
            "frequency": Decimal("1.5"),
        }
        for i in range(7)
    ]


def test_clearly_underperforming(snapshot):
    """Input: AS-102 Nov 3-9. Expected: underperforming, CPA 1701.32."""
    ad = snapshot["ad_sets"]["AS-102"]
    assert ad["flags"] == ["underperforming"]
    assert ad["metrics"]["cpa_inr"] == "1701.32"


def test_borderline_left_alone(snapshot):
    """Input: borderline AS-103. Expected: no performance flag."""
    assert snapshot["ad_sets"]["AS-103"]["flags"] == []


def test_real_creative_fatigue(snapshot):
    """Input: AS-104 rising frequency and falling CTR. Expected: fatigue flagged."""
    ad = snapshot["ad_sets"]["AS-104"]
    assert ad["flags"] == ["underperforming", "fatigue"]
    assert ad["metrics"]["frequency_early"] == "3.9200"
    assert ad["metrics"]["frequency_late"] == "4.6267"


@pytest.mark.parametrize("spend,revenue", [("960", "1000"), ("1000", "2400")])
def test_exact_threshold_does_not_flag(spend, revenue):
    """Input: exact CPA or ROAS threshold. Expected: strict comparisons leave it alone."""
    stats = calculate_window(synthetic_rows(spend=spend, revenue=revenue), DAY, POLICY)
    assert stats["flags"] == []


def test_three_days_ineligible(agent, snapshot):
    """Input: AS-204 with three observations. Expected: ineligible, no action."""
    ad = agent.snapshot("C2", DAY)["ad_sets"]["AS-204"]
    assert ad["metrics"]["observed_days"] == 3
    assert not ad["eligible"]
    assert not ad["flags"]


def test_missing_days_not_zero_filled(agent, db):
    """Input: AS-304 Oct 24-30 with two missing days. Expected: five days, no current-day action."""
    as_of = date(2026, 10, 30)
    ingest(db, "C3", as_of)
    ad = agent.snapshot("C3", as_of)["ad_sets"]["AS-304"]
    assert ad["metrics"]["observed_days"] == 5
    assert not ad["eligible"]
    assert db.one("SELECT count(*) AS n FROM daily_metrics WHERE ad_set_id='AS-304'")["n"] == 5


def test_duplicate_ingestion_and_daily_are_noops(agent, db):
    """Input: import/run Nov 9 twice. Expected: unchanged metrics, proposals, LLM calls."""
    first = agent.daily(DAY, execute=False)
    before = db.one("SELECT count(*) AS n FROM daily_metrics")["n"]
    calls = db.one("SELECT count(*) AS n FROM llm_calls")["n"]
    assert ingest(db, "C1", DAY) == 0
    second = agent.daily(DAY, execute=False)
    assert before == db.one("SELECT count(*) AS n FROM daily_metrics")["n"] == 73
    assert db.one("SELECT count(*) AS n FROM llm_calls")["n"] == calls == 3
    assert db.one("SELECT count(*) AS n FROM proposals")["n"] == 4
    assert all(c["status"] == "already_completed" for c in second["clients"])
    assert all(c["status"] == "completed" for c in first["clients"])


def test_no_rows_for_requested_day(agent, db):
    """Input: Nov 10, absent from CSV. Expected: no_data and no LLM calls/proposals."""
    result = agent.daily(date(2026, 11, 10), execute=False)
    assert all(c["status"] == "no_data" for c in result["clients"])
    assert db.one("SELECT count(*) AS n FROM llm_calls")["n"] == 0
    assert db.one("SELECT count(*) AS n FROM proposals")["n"] == 0


def test_zero_conversions_defined():
    """Input: positive material spend, zero conversions. Expected: null CPA and performance flag."""
    stats = calculate_window(synthetic_rows(conversions=0), DAY, POLICY)
    assert stats["metrics"]["cpa_inr"] is None
    assert stats["flags"] == ["underperforming"]


def test_zero_spend_or_impressions_defined():
    """Input: zero spend/impressions. Expected: null ratios, no exception or non-finite values."""
    stats = calculate_window(synthetic_rows(spend="0", impressions=0), DAY, POLICY)
    assert stats["metrics"]["roas"] is None
    assert stats["metrics"]["ctr_pct"] is None
    assert stats["flags"] == []


def test_future_rows_excluded():
    """Input: seven observed days plus extreme future data. Expected: unchanged seven-day result."""
    rows = synthetic_rows()
    before = calculate_window(rows, DAY, POLICY)
    rows.append({**rows[0], "date": DAY + timedelta(days=1), "spend_inr": Decimal("999999")})
    assert calculate_window(rows, DAY, POLICY) == before


def test_seed_does_not_reset_manual_changes(agent, db):
    """Input: manual edit followed by seed/restart. Expected: budget and revision survive."""
    agent.api.set_daily_budget("AS-102", 9000)
    db.migrate()
    seed(db)
    assert agent.api.get_ad_set("AS-102")["daily_budget_inr"] == 9000
    assert agent.api.get_ad_set("AS-102")["revision"] == 2


@pytest.mark.parametrize("invalid", ["-1", "NaN", "Infinity", "0.001"])
def test_bad_csv_values_fail_safely(db, tmp_path, invalid):
    """Input: invalid/non-finite/overprecise CSV spend. Expected: error with no inserted rows."""
    with (ROOT / "data/daily_metrics.csv").open(newline="") as handle:
        reader = csv.DictReader(handle)
        rows, fields = list(reader), reader.fieldnames
    rows[0]["spend_inr"] = invalid
    with (tmp_path / "daily_metrics.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError):
        ingest(db, "C1", DAY, tmp_path)
    assert db.one("SELECT count(*) AS n FROM daily_metrics")["n"] == 0


def test_changed_csv_cannot_rewrite_imported_evidence(db, tmp_path):
    """Input: changed imported CSV row. Expected: conflict; original evidence retained."""
    ingest(db, "C1", DAY)
    with (ROOT / "data/daily_metrics.csv").open(newline="") as handle:
        reader = csv.DictReader(handle)
        rows, fields = list(reader), reader.fieldnames
    for row in rows:
        if row["ad_set_id"] == "AS-102" and row["date"] == "2026-11-09":
            row["spend_inr"] = "1.00"
    with (tmp_path / "daily_metrics.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    before = db.one(
        "SELECT spend_inr FROM daily_metrics WHERE ad_set_id='AS-102' AND date=%s", (DAY,)
    )
    with pytest.raises(ValueError, match="Conflicting metrics"):
        ingest(db, "C1", DAY, tmp_path)
    assert (
        db.one("SELECT spend_inr FROM daily_metrics WHERE ad_set_id='AS-102' AND date=%s", (DAY,))
        == before
    )
