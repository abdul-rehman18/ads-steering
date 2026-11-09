"""Generate 60 days of mock data for a DTC brand advertising in 10 EU countries and the US.

ALL DATA IS FICTIONAL. Amounts are in EUR (US converted) to keep one reporting currency.

Files (data/mock/):
  shopify_orders.csv        source of truth for revenue. utm_source tells which channel brought the order
  google_campaign_daily.csv what Google says it spent and earned, per campaign per day
  meta_adset_daily.csv      what Meta says it spent and earned, per ad set per day
  meta_ad_daily.csv         ad level frequency and clicks, for creative fatigue
  meta_pixel_events.csv     Purchase events Meta received, with pixel id and event id
  merchant_center_feed.csv  products submitted and disapproved per country per day
  google_search_terms.csv   last 7 days of search terms
  change_history.csv        budget and setting changes made in the ad platforms
  budgets.yaml              monthly budget per country
  live_platform_state.json  current budgets in the platforms (what the mock API starts from)

Planted problems, on purpose:
  1. DE: Performance Max revenue collapses from 28 Oct because a feed update broke GTINs and most products got disapproved.
  2. FR: from 20 Oct a second Meta pixel sends every purchase again with its own event id, so Meta counts each sale twice.
  3. NL: on 1 Nov someone raised the NL Prospecting ad set budget in Meta by hand, so NL will overspend November.
Also, as everyday noise: US Prospecting over-delivers about 10% a day in November at a weak Shopify return, a small
drift the system may trim alone.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import pathlib
import random

import yaml

OUT = pathlib.Path(__file__).resolve().parent / "mock"
START = dt.date(2026, 9, 10)
DAYS = 60                                   # 10 Sep .. 8 Nov 2026
END = START + dt.timedelta(days=DAYS - 1)
random.seed(2026)

BUDGETS = {"DE": 60000, "FR": 45000, "NL": 25000, "DK": 18000, "SE": 18000, "IT": 30000,
           "ES": 28000, "AT": 12000, "BE": 14000, "PL": 10000, "US": 80000}
# share of the country budget per campaign / ad set
SPLIT = {"PMax": 0.40, "Search Brand": 0.10, "Prospecting": 0.35, "Retargeting": 0.15}
TRUE_ROAS = {"PMax": 3.1, "Search Brand": 7.5, "Prospecting": 2.3, "Retargeting": 4.2}
# platforms over-claim: view-through, cross-device, and both claiming the same order
CLAIM = {"PMax": 1.30, "Search Brand": 1.15, "Prospecting": 1.40, "Retargeting": 1.55}
AOV = {"US": 64.0}
DEFAULT_AOV = 58.0
ORGANIC_SHARE = 0.45   # organic + email + direct revenue relative to paid revenue

FEED_BREAK = dt.date(2026, 10, 28)
PIXEL_DOUBLE = dt.date(2026, 10, 20)
NL_RAISE = dt.date(2026, 11, 1)
NL_OLD, NL_NEW = None, 700.0  # NL_OLD filled from budgets below


def ids():
    n = 0
    while True:
        n += 1
        yield n


order_seq = ids()
event_seq = ids()


def daily_budget(country, campaign):
    return round(BUDGETS[country] * SPLIT[campaign] / 30, 0)


def main():
    global NL_OLD
    OUT.mkdir(parents=True, exist_ok=True)
    NL_OLD = daily_budget("NL", "Prospecting")

    orders, g_rows, m_rows, ad_rows, px_rows, feed_rows = [], [], [], [], [], []
    for i in range(DAYS):
        day = START + dt.timedelta(days=i)
        season = 1.0 + 0.25 * (i / DAYS)          # sales build towards November
        for c in BUDGETS:
            aov = AOV.get(c, DEFAULT_AOV)
            paid_rev = {}
            # ---- Google
            for camp in ("PMax", "Search Brand"):
                budget = daily_budget(c, camp)
                spend = budget * random.uniform(0.93, 1.01)
                roas = TRUE_ROAS[camp] * season * random.uniform(0.9, 1.1)
                if c == "DE" and camp == "PMax" and day >= FEED_BREAK:
                    days_in = (day - FEED_BREAK).days
                    serve = max(0.28, 0.75 - 0.08 * days_in)        # fewer products eligible, delivery falls
                    spend *= serve
                    roas *= 0.55                                     # only weak leftover products still serve
                rev = spend * roas
                paid_rev[("google", camp)] = rev
                claimed = rev * CLAIM[camp] * random.uniform(0.95, 1.05)
                g_rows.append({"date": day, "country": c, "campaign": f"{c} {camp}",
                               "campaign_type": "PERFORMANCE_MAX" if camp == "PMax" else "SEARCH",
                               "daily_budget": budget, "spend": round(spend, 2),
                               "impressions": int(spend * random.uniform(55, 70)),
                               "conversions": round(claimed / aov, 1), "conversion_value": round(claimed, 2)})
            # ---- Meta
            for adset in ("Prospecting", "Retargeting"):
                budget = daily_budget(c, adset)
                if c == "NL" and adset == "Prospecting" and day >= NL_RAISE:
                    budget = NL_NEW
                spend = budget * random.uniform(0.94, 1.01)
                roas = TRUE_ROAS[adset] * season * random.uniform(0.9, 1.1)
                if c == "US" and adset == "Prospecting" and day >= NL_RAISE:
                    spend = budget * random.uniform(1.09, 1.14)       # Meta over-delivers its daily budget
                    roas *= 0.72                                      # and the extra reach buys weaker buyers
                if c == "NL" and adset == "Prospecting" and day >= NL_RAISE:
                    roas *= 0.62                                     # extra spend buys weaker buyers
                rev = spend * roas
                paid_rev[("meta", adset)] = rev
                claimed = rev * CLAIM[adset] * random.uniform(0.95, 1.05)
                if c == "FR" and day >= PIXEL_DOUBLE:
                    claimed *= 2                                     # every purchase reported twice
                m_rows.append({"date": day, "country": c, "adset_id": f"as_{c.lower()}_{adset[:4].lower()}",
                               "adset": f"{c} {adset}", "daily_budget": budget, "spend": round(spend, 2),
                               "purchases": round(claimed / aov, 1), "purchase_value": round(claimed, 2)})
                # ad level, 2 ads per ad set
                for k in (1, 2):
                    imps = int(spend / 2 * random.uniform(80, 100))
                    freq = random.uniform(1.6, 2.4)
                    ctr = random.uniform(0.013, 0.018)
                    if c == "SE" and adset == "Prospecting" and k == 1:
                        t = i / DAYS                                     # one ad slowly wears out
                        freq = 1.9 + 4.2 * t ** 1.5
                        ctr = 0.017 - 0.011 * t ** 1.3
                    ad_rows.append({"date": day, "country": c, "adset": f"{c} {adset}",
                                    "ad": f"{c} {adset} ad {k}" + (" (bear video)" if k == 1 else " (gift box static)"),
                                    "impressions": imps, "clicks": int(imps * ctr), "frequency": round(freq, 2)})
            # ---- Shopify orders (truth)
            organic = sum(paid_rev.values()) * ORGANIC_SHARE * random.uniform(0.9, 1.1)
            buckets = [("google", camp, v) for (p, camp), v in paid_rev.items() if p == "google"] + \
                      [("facebook", adset, v) for (p, adset), v in paid_rev.items() if p == "meta"] + \
                      [("organic", "", organic * 0.6), ("email", "", organic * 0.25), ("direct", "", organic * 0.15)]
            for src, camp, value in buckets:
                n = max(0, int(round(value / aov)))
                for _ in range(n):
                    oid = f"#{100000 + next(order_seq)}"
                    ts = dt.datetime.combine(day, dt.time(random.randint(0, 23), random.randint(0, 59)))
                    total = round(aov * random.uniform(0.6, 1.5), 2)
                    orders.append({"order_id": oid, "created_at": ts.isoformat(), "country": c, "total_eur": total,
                                   "utm_source": src, "utm_campaign": f"{c} {camp}" if camp else ""})
                    if src == "facebook":
                        px = "px_main_1001"
                        px_rows.append({"date": day, "country": c, "pixel_id": px,
                                        "event_id": f"ev{next(event_seq)}", "order_id": oid})
                        if c == "FR" and day >= PIXEL_DOUBLE:
                            px_rows.append({"date": day, "country": c, "pixel_id": "px_theme_2002",
                                            "event_id": f"ev{next(event_seq)}", "order_id": oid})
            # ---- Merchant Center feed
            total_offers = 420 if c != "US" else 610
            bad = int(total_offers * random.uniform(0.02, 0.04))
            reason = "Image too small"
            if c == "DE" and day >= FEED_BREAK:
                bad = int(total_offers * min(0.66, 0.40 + 0.04 * (day - FEED_BREAK).days))
                reason = "Missing or invalid GTIN"
            feed_rows.append({"date": day, "country": c, "offers_submitted": total_offers,
                              "offers_disapproved": bad, "top_disapproval_reason": reason})

    write("shopify_orders.csv", orders)
    write("google_campaign_daily.csv", g_rows)
    write("meta_adset_daily.csv", m_rows)
    write("meta_ad_daily.csv", ad_rows)
    write("meta_pixel_events.csv", px_rows)
    write("merchant_center_feed.csv", feed_rows)
    write("google_search_terms.csv", search_terms())
    write("change_history.csv", [
        {"date": "2026-10-02", "platform": "google", "entity": "IT PMax", "field": "target_roas", "old": "3.5", "new": "3.2", "user": "freja@brand.example"},
        {"date": "2026-10-28", "platform": "merchant_center", "entity": "DE feed", "field": "feed_file", "old": "feed_de_v41.xml", "new": "feed_de_v42.xml", "user": "feed-app"},
        {"date": str(NL_RAISE), "platform": "meta", "entity": "NL Prospecting", "field": "daily_budget", "old": str(NL_OLD), "new": str(NL_NEW), "user": "jonas@brand.example"},
    ])
    (OUT / "budgets.yaml").write_text(yaml.safe_dump({"currency": "EUR", "monthly": BUDGETS, "split": SPLIT}, sort_keys=False))
    state = {"google": {}, "meta": {}}
    for c in BUDGETS:
        for camp in ("PMax", "Search Brand"):
            state["google"][f"{c} {camp}"] = {"id": f"cmp_{c.lower()}_{camp[:4].lower().strip()}", "status": "ENABLED",
                                              "daily_budget": daily_budget(c, camp)}
        for adset in ("Prospecting", "Retargeting"):
            b = NL_NEW if (c, adset) == ("NL", "Prospecting") else daily_budget(c, adset)
            state["meta"][f"{c} {adset}"] = {"id": f"as_{c.lower()}_{adset[:4].lower()}", "status": "ACTIVE", "daily_budget": b}
    (OUT / "live_platform_state.json").write_text(json.dumps(state, indent=2))
    (OUT / "README.txt").write_text("Fictional mock data generated by data/generate.py. Amounts in EUR.\n"
                                    f"Covers {START} to {END}.\n")
    print(f"{len(orders)} Shopify orders, {len(g_rows)} Google rows, {len(m_rows)} Meta rows, "
          f"{len(px_rows)} pixel events, {START} to {END}")


def search_terms():
    rows = []
    good = ["weighted plush bear", "weighted teddy bear", "sensory plush toy", "calming stuffed animal",
            "gift for anxious kid", "plush whale big"]
    waste = [("DE", "plüschtier schnittmuster kostenlos"), ("US", "free plush toy sewing pattern"),
             ("US", "plush toy factory jobs"), ("FR", "peluche lestée fabrication diy"), ("NL", "knuffel tekenen")]
    for c in BUDGETS:
        for t in good:
            clicks = random.randint(40, 220)
            cost = round(clicks * random.uniform(0.45, 0.9), 2)
            rows.append({"week_ending": str(END), "country": c, "campaign": f"{c} PMax", "search_term": t,
                         "clicks": clicks, "cost": cost, "conversions": round(clicks * random.uniform(0.02, 0.05), 1)})
    for c, t in waste:
        clicks = random.randint(180, 420)
        rows.append({"week_ending": str(END), "country": c, "campaign": f"{c} PMax", "search_term": t,
                     "clicks": clicks, "cost": round(clicks * random.uniform(0.5, 0.8), 2), "conversions": 0})
    return rows


def write(name, rows):
    with (OUT / name).open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        for r in rows:
            w.writerow({k: (str(v) if isinstance(v, dt.date) else v) for k, v in r.items()})


if __name__ == "__main__":
    main()
