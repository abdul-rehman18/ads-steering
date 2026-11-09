"""When a campaign's Shopify revenue collapses, list every likely cause and test each one against the data.

The output is never a single guess. Each cause is marked SUPPORTED, RULED OUT or CANNOT TELL, with the numbers
that decided it, and for CANNOT TELL, the data that would settle it.
"""
from __future__ import annotations

import datetime as dt

from steer.data import days_ago


def find_collapses(data, end: str, drop_pct=40) -> list[dict]:
    """Campaigns whose Shopify revenue in the last 7 days fell sharply against the 4 weeks before."""
    out = []
    for r in {(x["platform"], x["entity"], x["country"]) for x in data.entity_rows()}:
        platform, entity, country = r
        utm = "google" if platform == "google" else "facebook"
        now = data.shopify_revenue(days_ago(end, 6), end, utm_campaign=entity) / 7
        base = data.shopify_revenue(days_ago(end, 34), days_ago(end, 7), utm_campaign=entity) / 28
        if base > 0 and (base - now) / base * 100 >= drop_pct:
            out.append({"platform": platform, "entity": entity, "country": country,
                        "shopify_per_day_before": round(base), "shopify_per_day_now": round(now),
                        "drop_pct": round((base - now) / base * 100)})
    return sorted(out, key=lambda x: -x["drop_pct"])


def first_drop_day(data, entity, end, base_per_day, start_back=30):
    for i in range(start_back, -1, -1):
        d = days_ago(end, i)
        if data.shopify_revenue(d, d, utm_campaign=entity) < base_per_day * 0.6:
            return d
    return None


def investigate(data, collapse: dict) -> dict:
    end = data.last_date
    entity, country, platform = collapse["entity"], collapse["country"], collapse["platform"]
    utm = "google" if platform == "google" else "facebook"
    recent = (days_ago(end, 6), end)
    started = first_drop_day(data, entity, end, collapse["shopify_per_day_before"])
    # "Before" means before the drop began, so a slow collapse does not hide inside the baseline.
    before_end = days_ago(started, 1) if started else days_ago(end, 7)
    before = (days_ago(before_end, 27), before_end)
    causes = []

    def add(name, status, evidence, would_confirm=None):
        causes.append({"cause": name, "status": status, "evidence": evidence, "would_confirm": would_confirm})

    # 1. Product feed
    feed = [f for f in data.feed if f["country"] == country]
    def share(a, b):
        rows = [f for f in feed if a <= f["date"] <= b]
        sub = sum(int(f["offers_submitted"]) for f in rows) or 1
        return sum(int(f["offers_disapproved"]) for f in rows) / sub
    s_before, s_now = share(*before), share(*recent)
    feed_jump = next((f["date"] for f in sorted(feed, key=lambda f: f["date"])
                      if int(f["offers_disapproved"]) / int(f["offers_submitted"]) > 0.2), None)
    reasons = {f["top_disapproval_reason"] for f in feed if recent[0] <= f["date"] <= recent[1]}
    feed_changes = [c for c in data.changes if c["platform"] == "merchant_center" and country in c["entity"]]
    if s_now > 0.2 and s_now > 3 * s_before:
        ev = (f"Disapproved products went from {s_before:.0%} to {s_now:.0%} of the {country} feed. "
              f"First day above 20%: {feed_jump}. Revenue started falling: {started}. Top reason: {', '.join(sorted(reasons))}.")
        if feed_changes:
            c = feed_changes[-1]
            ev += f" The feed file changed on {c['date']} ({c['old']} to {c['new']}, by {c['user']})."
        add("Products disapproved in Merchant Center", "SUPPORTED", ev)
    else:
        add("Products disapproved in Merchant Center", "RULED OUT",
            f"Disapproved share {s_before:.0%} before, {s_now:.0%} now.")

    # 2. Someone changed the campaign
    changes = [c for c in data.changes if c["entity"] == entity and c["date"] >= before[0]]
    if changes:
        add("Budget, bid or target changed", "SUPPORTED", "; ".join(f"{c['date']} {c['field']} {c['old']} to {c['new']} by {c['user']}" for c in changes))
    else:
        add("Budget, bid or target changed", "RULED OUT", f"No changes to {entity} in the platform change history since {before[0]}.")

    # 3. Not enough budget
    rows = [r for r in data.entity_rows() if r["entity"] == entity and recent[0] <= r["date"] <= recent[1]]
    used = sum(r["spend"] for r in rows) / (sum(r["daily_budget"] for r in rows) or 1)
    if used >= 0.9:
        add("Held back by its budget", "SUPPORTED", f"Spent {used:.0%} of its budget in the last 7 days.")
    else:
        add("Held back by its budget", "RULED OUT",
            f"Spent only {used:.0%} of its budget in the last 7 days, so it is not short of money. It cannot find enough to show.")

    # 4. Tracking broke (the platform stopped seeing sales that Shopify still gets)
    shop_before = data.shopify_revenue(*before, utm_campaign=entity) / 28
    shop_ratio = data.shopify_revenue(*recent, utm_campaign=entity) / 7 / max(shop_before, 1)
    plat_now = data.platform_claimed(*recent, platform, entity=entity) / 7
    plat_before = data.platform_claimed(*before, platform, entity=entity) / 28
    plat_ratio = plat_now / plat_before if plat_before else 0
    if abs(shop_ratio - plat_ratio) < 0.25:
        add("Tracking broke", "RULED OUT",
            f"Shopify and {platform.title()} both fell by a similar amount ({1 - shop_ratio:.0%} and {1 - plat_ratio:.0%}). The sales really fell.")
    else:
        add("Tracking broke", "SUPPORTED", f"Shopify fell {1 - shop_ratio:.0%} but {platform.title()} reports {1 - plat_ratio:.0%}.")

    # 5. The shop itself (checkout, site down): other channels in the same country would fall too
    others_now = (data.shopify_revenue(*recent, country=country) - data.shopify_revenue(*recent, utm_campaign=entity)) / 7
    others_before = (data.shopify_revenue(*before, country=country) - data.shopify_revenue(*before, utm_campaign=entity)) / 28
    change = others_now / others_before - 1 if others_before else 0
    if change > -0.15:
        add("Shop or checkout problem in " + country, "RULED OUT",
            f"Shopify revenue from every other source in {country} changed {change:+.0%}.")
    else:
        add("Shop or checkout problem in " + country, "SUPPORTED", f"Other sources in {country} fell {change:.0%} too.")

    # 6. Market wide drop: the same campaign type in other countries
    kind = entity.split(" ", 1)[1]
    other = [c for c in data.budgets if c != country]
    o_now = sum(data.shopify_revenue(*recent, utm_campaign=f"{c} {kind}") for c in other) / 7
    o_before = sum(data.shopify_revenue(*before, utm_campaign=f"{c} {kind}") for c in other) / 28
    oc = o_now / o_before - 1 if o_before else 0
    if oc > -0.15:
        add("Seasonal or market wide drop", "RULED OUT", f"{kind} in the other {len(other)} countries changed {oc:+.0%}.")
    else:
        add("Seasonal or market wide drop", "SUPPORTED", f"{kind} fell {oc:.0%} across other countries too.")

    # 7 and 8. Things this data cannot answer. Said plainly, with what would answer them.
    add("Best sellers out of stock", "CANNOT TELL", "No stock levels per product in this data.",
        "Shopify inventory by product for the drop dates, joined to the products PMax was showing.")
    add("Competitors bidding harder", "CANNOT TELL", "No auction data in this export.",
        "Google Ads auction insights (impression share, outranking share) for the campaign, before and after.")

    order = {"SUPPORTED": 0, "CANNOT TELL": 1, "RULED OUT": 2}
    causes.sort(key=lambda c: order[c["status"]])
    now_per_day = data.shopify_revenue(*recent, utm_campaign=entity) / 7
    drop = round((shop_before - now_per_day) / shop_before * 100) if shop_before else collapse["drop_pct"]
    return {**collapse, "shopify_per_day_before": round(shop_before), "shopify_per_day_now": round(now_per_day),
            "drop_pct": drop, "drop_started": started, "causes": causes}
