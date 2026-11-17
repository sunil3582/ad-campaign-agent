"""Import the supplied CSVs and detect problems with explicit, testable rules."""

import csv
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from guardian.db import ROOT

METRIC_FIELDS = ("spend_inr", "impressions", "clicks", "conversions", "revenue_inr", "frequency")


def read_csv(path, columns):
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if set(reader.fieldnames or []) != set(columns):
            raise ValueError(f"Unexpected CSV columns in {Path(path).name}")
        rows = list(reader)
    if any(None in row or any(v is None for v in row.values()) for row in rows):
        raise ValueError(f"Incomplete CSV row in {Path(path).name}")
    return rows


def nonnegative(value, *, integer=False):
    number = Decimal(value)
    if not number.is_finite() or number < 0:
        raise ValueError("Metric values must be finite and nonnegative")
    if integer and number != number.to_integral_value():
        raise ValueError("Counts and budget commands must be whole numbers")
    return int(number) if integer else number


def seed(db, data_dir=None):
    """Insert initial identities/platform state once. Never reset a manual edit."""
    folder = Path(data_dir or ROOT / "data")
    clients = read_csv(
        folder / "clients.csv",
        (
            "client_id",
            "name",
            "target_cpa_inr",
            "target_roas",
            "daily_budget_cap_inr",
            "max_shift_pct",
        ),
    )
    ads = read_csv(
        folder / "ad_sets.csv",
        (
            "ad_set_id",
            "client_id",
            "platform",
            "name",
            "creative_id",
            "daily_budget_inr",
            "status",
        ),
    )
    for rows, key in ((clients, "client_id"), (ads, "ad_set_id")):
        if len({r[key] for r in rows}) != len(rows):
            raise ValueError(f"Duplicate {key} in seed CSV")
    with db.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('guardian:seed'))")
        for r in clients:
            conn.execute(
                """INSERT INTO clients(client_id,name,target_cpa_inr,target_roas,
                   daily_budget_cap_inr,max_shift_pct) VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (client_id) DO NOTHING""",
                (
                    r["client_id"],
                    r["name"],
                    nonnegative(r["target_cpa_inr"]),
                    nonnegative(r["target_roas"]),
                    nonnegative(r["daily_budget_cap_inr"], integer=True),
                    nonnegative(r["max_shift_pct"]),
                ),
            )
        for r in ads:
            conn.execute(
                """INSERT INTO ad_sets(ad_set_id,client_id,platform,name,creative_id)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT (ad_set_id) DO NOTHING""",
                (r["ad_set_id"], r["client_id"], r["platform"], r["name"], r["creative_id"]),
            )
            conn.execute(
                """INSERT INTO mock_ad_sets(ad_set_id,client_id,daily_budget_inr,status,creative_id)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT (ad_set_id) DO NOTHING""",
                (
                    r["ad_set_id"],
                    r["client_id"],
                    nonnegative(r["daily_budget_inr"], integer=True),
                    r["status"],
                    r["creative_id"],
                ),
            )


def ingest(db, client_id, as_of, data_dir=None):
    """Backfill only D-6..D; reject conflicting repeats instead of rewriting evidence."""
    folder = Path(data_dir or ROOT / "data")
    rows = read_csv(folder / "daily_metrics.csv", ("date", "ad_set_id", *METRIC_FIELDS))
    start = as_of - timedelta(days=6)
    seen = set()
    parsed = []
    for row in rows:
        day = date.fromisoformat(row["date"])
        key = (row["ad_set_id"], day)
        if key in seen:
            raise ValueError(f"Duplicate metric key: {key}")
        seen.add(key)
        values = {
            field: nonnegative(
                row[field], integer=field in ("impressions", "clicks", "conversions")
            )
            for field in METRIC_FIELDS
        }
        for field, precision in (
            ("spend_inr", ".01"),
            ("revenue_inr", ".01"),
            ("frequency", ".0001"),
        ):
            if values[field] != values[field].quantize(Decimal(precision)):
                raise ValueError(f"Unsupported precision for {field}: {key}")
        if values["clicks"] > values["impressions"]:
            raise ValueError(f"Clicks exceed impressions: {key}")
        if start <= day <= as_of:
            parsed.append({"ad_set_id": row["ad_set_id"], "date": day, **values})
    with db.connect() as conn:
        owners = {
            r["ad_set_id"]: r["client_id"]
            for r in conn.execute("SELECT ad_set_id,client_id FROM ad_sets")
        }
        unknown = {r["ad_set_id"] for r in parsed} - owners.keys()
        if unknown:
            raise ValueError(f"Unknown metric ad sets: {sorted(unknown)}")
        imported = 0
        for row in parsed:
            if owners[row["ad_set_id"]] != client_id:
                continue
            existing = conn.execute(
                "SELECT * FROM daily_metrics WHERE ad_set_id=%s AND date=%s",
                (row["ad_set_id"], row["date"]),
            ).fetchone()
            if existing and any(existing[k] != row[k] for k in METRIC_FIELDS):
                raise ValueError(f"Conflicting metrics for {row['ad_set_id']} on {row['date']}")
            inserted = conn.execute(
                """INSERT INTO daily_metrics(ad_set_id,date,spend_inr,impressions,clicks,
                   conversions,revenue_inr,frequency) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (ad_set_id,date) DO NOTHING RETURNING ad_set_id""",
                (row["ad_set_id"], row["date"], *(row[k] for k in METRIC_FIELDS)),
            ).fetchone()
            imported += bool(inserted)
    return imported


def number(value, places="0.0001"):
    return None if value is None else str(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def calculate_window(rows, as_of, policy):
    start = as_of - timedelta(days=6)
    rows = [r for r in rows if start <= r["date"] <= as_of]

    def total(field):
        return sum((Decimal(r[field]) for r in rows), Decimal(0))

    spend, revenue = total("spend_inr"), total("revenue_inr")
    conversions, impressions, clicks = total("conversions"), total("impressions"), total("clicks")
    cpa = spend / conversions if conversions else None
    roas = revenue / spend if spend else None
    ctr = clicks / impressions * 100 if impressions else None

    def segment(lo, hi):
        group = [r for r in rows if lo <= r["date"] <= hi]
        imp = sum(Decimal(r["impressions"]) for r in group)
        frequency = sum(Decimal(r["frequency"]) for r in group) / len(group) if group else None
        ctr = sum(Decimal(r["clicks"]) for r in group) / imp * 100 if imp else None
        return len(group), frequency, ctr

    early_n, early_f, early_ctr = segment(start, as_of - timedelta(days=4))
    late_n, late_f, late_ctr = segment(as_of - timedelta(days=2), as_of)
    days = len({r["date"] for r in rows})
    eligible = days >= 5 and any(r["date"] == as_of for r in rows)
    target_cpa = Decimal(policy["target_cpa_inr"])
    target_roas = Decimal(policy["target_roas"])
    bad_cpa = cpa > target_cpa * Decimal("1.2") if cpa is not None else spend >= 2 * target_cpa
    under = bool(spend > 0 and bad_cpa and roas is not None and roas < target_roas * Decimal(".8"))
    fatigue = bool(
        early_n >= 2
        and late_n >= 2
        and early_f
        and early_ctr
        and late_f >= 3
        and late_f >= early_f * Decimal("1.15")
        and late_ctr is not None
        and late_ctr <= early_ctr * Decimal(".8")
    )
    flags = []
    if eligible and under:
        flags.append("underperforming")
    if eligible and fatigue:
        flags.append("fatigue")
    evidence = {
        "window_start": start.isoformat(),
        "window_end": as_of.isoformat(),
        "observed_days": days,
        "spend_inr": number(spend, ".01"),
        "revenue_inr": number(revenue, ".01"),
        "impressions": int(impressions),
        "clicks": int(clicks),
        "conversions": int(conversions),
        "cpa_inr": number(cpa, ".01"),
        "roas": number(roas),
        "ctr_pct": number(ctr),
        "frequency_early": number(early_f),
        "frequency_late": number(late_f),
        "ctr_early_pct": number(early_ctr),
        "ctr_late_pct": number(late_ctr),
    }
    healthy = bool(
        eligible
        and cpa is not None
        and cpa <= target_cpa
        and roas is not None
        and roas >= target_roas
        and not fatigue
    )
    return {"metrics": evidence, "eligible": eligible, "flags": flags, "healthy": healthy}


def build_snapshot(conn, client_id, as_of, live):
    client = conn.execute("SELECT * FROM clients WHERE client_id=%s", (client_id,)).fetchone()
    if not client:
        raise ValueError(f"Unknown client: {client_id}")
    policy = {
        k: str(client[k]) if isinstance(client[k], Decimal) else client[k]
        for k in (
            "target_cpa_inr",
            "target_roas",
            "daily_budget_cap_inr",
            "max_shift_pct",
            "revision",
        )
    }
    rows = conn.execute(
        """SELECT m.* FROM daily_metrics m JOIN ad_sets a USING(ad_set_id)
           WHERE a.client_id=%s AND m.date BETWEEN %s AND %s ORDER BY m.date""",
        (client_id, as_of - timedelta(days=6), as_of),
    ).fetchall()
    sets = {}
    for state in live:
        if state["client_id"] != client_id:
            raise ValueError("Platform returned another client's state")
        stats = calculate_window(
            [r for r in rows if r["ad_set_id"] == state["ad_set_id"]], as_of, policy
        )
        if state["status"] != "active":
            stats.update(eligible=False, flags=[], healthy=False)
        sets[state["ad_set_id"]] = {**state, **stats}
    return {"client_id": client_id, "as_of": as_of.isoformat(), "policy": policy, "ad_sets": sets}
