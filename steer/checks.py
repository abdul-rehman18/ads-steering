"""Checks that decide whether the data can be trusted before anyone steers on it."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from steer.data import days_ago


def freshness(data, today: dt.date, max_hours: int) -> dict:
    """Each source's newest day must be yesterday or close to it. A stale source freezes steering."""
    out = {}
    for src, last in data.last_date_by_source().items():
        age_h = (dt.datetime.combine(today, dt.time(6)) - dt.datetime.combine(dt.date.fromisoformat(last), dt.time(23, 59))).total_seconds() / 3600
        # Stale if yesterday is missing, or if the newest day is older than the hour limit.
        missing_yesterday = dt.date.fromisoformat(last) < today - dt.timedelta(days=1)
        out[src] = {"last_day": last, "age_hours": round(age_h, 1), "stale": missing_yesterday or age_h > max_hours}
    return out


def tracking(data, end: str) -> list[dict]:
    """Compare what each platform claims with the orders Shopify actually got from it.

    Platforms always claim more than Shopify credits them (view-through, cross-device, overlap), so the absolute
    ratio is not the signal. A sudden jump in the ratio, with Shopify flat, is.
    """
    issues = []
    recent = (days_ago(end, 6), end)
    base = (days_ago(end, 41), days_ago(end, 21))
    for country in sorted(data.budgets):
        for platform, utm in (("meta", "facebook"), ("google", "google")):
            def ratio(a, b):
                claimed = data.platform_claimed(a, b, platform, country=country, field="claimed_count")
                real = data.shopify_orders(a, b, country=country, utm_source=utm)
                return (claimed / real if real else None), claimed, real
            r_now, c_now, s_now = ratio(*recent)
            r_base, c_base, s_base = ratio(*base)
            if r_now and r_base and r_now / r_base > 1.6:
                issue = {"country": country, "platform": platform, "ratio_before": round(r_base, 2),
                         "ratio_now": round(r_now, 2), "claimed_last_7d": round(c_now), "shopify_orders_last_7d": s_now}
                spend = sum(r["spend"] for r in data.entity_rows()
                    if r["platform"] == platform and r["country"] == country and recent[0] <= r["date"] <= recent[1])
                issue["platform_roas_7d"] = round(data.platform_claimed(*recent, platform, country=country) / spend, 2)
                issue["shopify_roas_7d"] = round(data.shopify_revenue(*recent, country=country, utm_source=utm) / spend, 2)
                if platform == "meta":
                    issue.update(pixel_duplicates(data, country, *recent))
                issues.append(issue)
    return issues


def pixel_duplicates(data, country, start, end) -> dict:
    per_order = defaultdict(set)
    first_seen = {}
    for e in data.pixel:
        if e["country"] == country and start <= e["date"] <= end:
            per_order[e["order_id"]].add(e["pixel_id"])
    doubled = [o for o, px in per_order.items() if len(px) > 1]
    pixels = sorted({p for px in per_order.values() for p in px})
    for e in data.pixel:
        if e["country"] == country and e["pixel_id"] not in first_seen:
            first_seen[e["pixel_id"]] = e["date"]
    return {"orders_checked": len(per_order), "orders_sent_by_two_pixels": len(doubled), "pixels": pixels,
            "pixel_first_seen": {p: first_seen[p] for p in pixels}}
