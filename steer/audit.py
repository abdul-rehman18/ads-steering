"""Weekly audit. Each finding goes into the queue with an owner and a deadline."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from steer.checks import tracking
from steer.data import days_ago
from steer.llm import judge_terms


def run(data, queue, today: dt.date, claude=None, api=None, cfg=None) -> dict:
    end = data.last_date
    out = {}

    # 1. Wasted spend and negatives
    waste = [t for t in data.terms if float(t["conversions"]) == 0 and float(t["cost"]) >= 50]
    out["wasted_spend"] = {"terms": len(waste), "cost": round(sum(float(t["cost"]) for t in waste), 2)}
    for t in judge_terms(claude, waste):
        queue.propose("add_negative", f"Add negative '{t['negative']}' on {t['campaign']}",
                      f"Search term '{t['search_term']}' cost {float(t['cost']):.2f} EUR over {t['clicks']} clicks in 7 days "
                      f"with no sales. {t['why']}",
                      {"search_term": t["search_term"], "negative": t["negative"], "match_type": "PHRASE",
                       "clicks": t["clicks"], "cost": t["cost"]},
                      "approval", dedupe_key=f"neg:{t['campaign']}:{t['negative']}", platform="google",
                      entity=t["campaign"], country=t["country"])

    # 2. Feed disapprovals
    latest = [f for f in data.feed if f["date"] == end]
    bad = [f for f in latest if int(f["offers_disapproved"]) / int(f["offers_submitted"]) > 0.10]
    out["feed"] = [{"country": f["country"], "disapproved": int(f["offers_disapproved"]),
                    "submitted": int(f["offers_submitted"]), "reason": f["top_disapproval_reason"]} for f in bad]
    for f in out["feed"]:
        queue.propose("fix_feed", f"Fix {f['disapproved']} disapproved products in the {f['country']} feed",
                      f"{f['disapproved']} of {f['submitted']} products are disapproved ({f['reason']}). "
                      f"Disapproved products cannot show in Shopping or Performance Max.",
                      f, "manual", dedupe_key=f"feed:{f['country']}", country=f["country"])

    # 3. Tracking
    out["tracking"] = tracking(data, end)
    raise_tracking_tasks(queue, out["tracking"])

    # 4. Creative fatigue
    out["fatigue"] = []
    ads = defaultdict(list)
    for r in data.meta_ads:
        ads[r["ad"]].append(r)
    for ad, rows in ads.items():
        rows.sort(key=lambda r: r["date"])
        last7, first14 = rows[-7:], rows[:14]
        def ctr(rs):
            return sum(int(r["clicks"]) for r in rs) / max(sum(int(r["impressions"]) for r in rs), 1)
        freq = sum(float(r["frequency"]) for r in last7) / 7
        drop = 1 - ctr(last7) / ctr(first14)
        if freq > 4 and drop > 0.4:
            f = {"ad": ad, "country": rows[0]["country"], "frequency_7d": round(freq, 1),
                 "ctr_start": round(ctr(first14) * 100, 2), "ctr_now": round(ctr(last7) * 100, 2)}
            out["fatigue"].append(f)
            queue.propose("refresh_creative", f"Refresh '{ad}'",
                          f"People now see it {f['frequency_7d']} times a week and the click rate fell from "
                          f"{f['ctr_start']}% to {f['ctr_now']}%.", f, "manual", dedupe_key=f"fatigue:{ad}",
                          country=f["country"])

    # 5. Pacing
    if api is not None and cfg is not None:
        from steer.steering import pacing
        tol = cfg["rules"]["pacing_tolerance_pct"]
        out["pacing"] = [{"country": r["country"], "pace_pct": r["pace_pct"], "projected": r["projected"],
                          "budget": r["budget"]} for r in pacing(data, api, cfg) if abs(r["pace_pct"] - 100) > tol]

    # 6. Platforms claim the same orders, so their ROAS cannot be added. Measured only where tracking is healthy.
    start = days_ago(end, 27)
    broken = {t["country"] for t in out["tracking"]}
    claims = []
    for c in data.budgets:
        if c in broken:
            continue
        shop_paid = data.shopify_revenue(start, end, country=c, utm_source="google") + \
                    data.shopify_revenue(start, end, country=c, utm_source="facebook")
        claimed = data.platform_claimed(start, end, "google", country=c) + data.platform_claimed(start, end, "meta", country=c)
        claims.append(round(claimed / shop_paid, 2))
    claims.sort()
    out["overclaim"] = {"median_x": claims[len(claims) // 2], "low_x": claims[0], "high_x": claims[-1],
                        "countries": len(claims)}
    return out


def raise_tracking_tasks(queue, issues):
    for t in issues:
        extra = ""
        if t.get("orders_sent_by_two_pixels"):
            extra = (f" {t['orders_sent_by_two_pixels']} of {t['orders_checked']} orders reached Meta from two pixels "
                     f"({', '.join(t['pixels'])}) with different event ids, so Meta cannot remove the duplicate.")
        queue.propose("fix_tracking", f"{t['country']} {t['platform'].title()} is counting purchases twice",
                      f"{t['platform'].title()} now reports {t['ratio_now']}x the orders Shopify credits it, up from "
                      f"{t['ratio_before']}x.{extra} Raises in {t['country']} are frozen until fixed.",
                      t, "manual", dedupe_key=f"tracking:{t['country']}:{t['platform']}", country=t["country"])
