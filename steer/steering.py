"""Daily steering: keep each country on its monthly budget, judged on Shopify revenue, inside the hard rules.

How it decides, in order:
1. A budget someone raised by hand outside the rules is put back to its last allowed value first.
2. A country on course to overspend its month is brought back by lowering every campaign in it by the same
   share, so no single ad set is starved and pushed back into learning.
3. A campaign spending above its daily budget while earning below the Shopify floor is trimmed a little.
4. A country under its month gets a raise only on a campaign that is spending its budget and earns well on Shopify,
   and only where nothing is frozen.
Every number is projected from what is live now (current budgets times how much of them each campaign really spends),
not from a simple run rate.
"""
from __future__ import annotations

import calendar
import datetime as dt

from steer.data import days_ago, month_bounds
from steer.rules import check_budget_change, tier_for


def base_daily(data, country, entity, days_in_month):
    kind = entity.split(" ", 1)[1]
    return data.budgets[country] * data.split[kind] / days_in_month


def entities(data, api, country, end, days_in_month, first):
    out = []
    for platform, entity in sorted({(r["platform"], r["entity"]) for r in data.entity_rows() if r["country"] == country}):
        live = api.get(platform, entity)
        rows = [r for r in data.entity_rows() if r["entity"] == entity and days_ago(end, 6) <= r["date"] <= end]
        delivery = sum(r["spend"] for r in rows) / (sum(r["daily_budget"] for r in rows) or 1)
        utm = "google" if platform == "google" else "facebook"
        spend7 = sum(r["spend"] for r in rows)
        roas7 = data.shopify_revenue(days_ago(end, 6), end, utm_campaign=entity) / spend7 if spend7 else 0
        out.append({"platform": platform, "entity": entity, "live_budget": live, "delivery": delivery,
                    "mtd": data.spend_between(first.isoformat(), end, entity=entity),
                    "base": base_daily(data, country, entity, days_in_month), "shopify_roas_7d": roas7, "utm": utm})
    return out


def pacing(data, api, cfg):
    """Pacing for every country, with no side effects. Used by the daily run and the weekly audit."""
    end = data.last_date
    d = dt.date.fromisoformat(end)
    first, _ = month_bounds(d)
    days_in_month = calendar.monthrange(d.year, d.month)[1]
    elapsed = (d - first).days + 1
    remaining = days_in_month - elapsed
    rows = []
    for country, budget in data.budgets.items():
        ents = entities(data, api, country, end, days_in_month, first)
        mtd = data.spend_between(first.isoformat(), end, country=country)
        projected = mtd + sum(e["live_budget"] * min(e["delivery"], 1.25) for e in ents) * remaining
        rows.append({"country": country, "budget": budget, "mtd_spend": round(mtd), "projected": round(projected),
                     "pace_pct": round(projected / budget * 100), "remaining_days": remaining, "elapsed": elapsed,
                     "shopify_revenue_mtd": round(data.shopify_revenue(first.isoformat(), end, country=country)),
                     "entities": ents, "month": first})
    return rows


def run(data, queue, api, cfg, today: dt.date, frozen: dict) -> list[dict]:
    rules = cfg["rules"]
    tol = rules["pacing_tolerance_pct"] / 100
    report = []
    hand_raises = _hand_raises(data, cfg, today)
    for row in pacing(data, api, cfg):
        country, budget, ents, remaining = row["country"], row["budget"], row["entities"], row["remaining_days"]
        row["mer"] = round(row["shopify_revenue_mtd"] / row["mtd_spend"], 2) if row["mtd_spend"] else None
        row["frozen"] = frozen.get(country)
        row["action"] = "on plan"

        # 1. Put back a raise made by hand that broke the rules.
        reverted = False
        for e in ents:
            hr = hand_raises.get(e["entity"])
            if hr and abs(e["live_budget"] - hr["new"]) < 0.01:
                after = row["mtd_spend"] + sum((hr["old"] if x is e else x["live_budget"]) * min(x["delivery"], 1.0)
                                               for x in ents) * remaining
                gap_share = 1 - (budget - row["mtd_spend"]) / max(after - row["mtd_spend"], 1)
                _propose(queue, cfg, today, country, e, hr["old"], kind_note="revert",
                         title=f"Put {e['entity']} back to {hr['old']:.0f} EUR a day (raised by hand to {hr['new']:.0f})",
                         reason=(f"{e['entity']} was raised from {hr['old']:.0f} to {hr['new']:.0f} EUR a day on {hr['date']} by "
                                 f"{hr['user']}, outside the system. That is {hr['step']:+.0f}% in one step against the "
                                 f"{rules['max_raise_pct']}% rule, by someone who cannot approve money. Since then its Shopify "
                                 f"return fell to {e['shopify_roas_7d']:.2f}, below the {rules['min_shopify_roas_to_raise']} floor. "
                                 f"Going back to {hr['old']:.0f} is the level that worked. {country} still ends the month near "
                                 f"{after:,.0f} EUR ({after / budget - 1:+.0%}) because of the days already spent. Landing exactly "
                                 f"on {budget:,.0f} EUR would need every {country} budget about {max(gap_share, 0):.0%} lower for "
                                 f"the rest of the month. If this is approved, tomorrow's run proposes that, spread evenly."),
                         evidence={"hand_change": hr, "country_mtd": row["mtd_spend"], "projected_now": row["projected"],
                                   "projected_after_revert": round(after), "budget": budget,
                                   "shopify_roas_7d": round(e["shopify_roas_7d"], 2), "days_left": remaining})
                queue.propose("investigate", f"Talk to {hr['user'].split('@')[0].title()} about the {e['entity']} budget change",
                              f"Raised {hr['step']:+.0f}% by hand on {hr['date']}, outside the queue and the rules.",
                              hr, "manual", dedupe_key=f"handraise:{e['entity']}:{hr['date']}", country=country)
                row["action"] = f"revert hand raise on {e['entity']}"
                reverted = True
        if reverted:
            report.append(row)
            continue

        # 2. Country will overspend: lower everything in it by the same share.
        if row["projected"] > budget * (1 + tol) and remaining > 0:
            future = row["projected"] - row["mtd_spend"]
            share = 1 - (budget - row["mtd_spend"]) / future if future else 0
            for e in ents:
                new = round(max(rules["min_daily_budget_eur"], e["live_budget"] * (1 - share)), 2)
                _propose(queue, cfg, today, country, e, new,
                         title=f"Lower {e['entity']} from {e['live_budget']:.0f} to {new:.0f} EUR a day ({-share:.0%})",
                         reason=(f"{country} is on course for {row['projected']:,.0f} EUR against {budget:,.0f}. Every {country} "
                                 f"budget goes down by the same {share:.0%}, so no single campaign is starved."),
                         evidence={"projected": row["projected"], "budget": budget, "share_cut": round(share, 3)})
            row["action"] = f"lower all {country} budgets {share:.0%}"
            report.append(row)
            continue

        # 3. Over-delivering and earning below the floor: trim a little.
        for e in ents:
            if e["delivery"] > 1.05 and e["shopify_roas_7d"] < rules["min_shopify_roas_to_raise"] and remaining > 0:
                new = round(e["live_budget"] * (1 - rules["auto_lower_max_pct"] / 100), 2)
                _propose(queue, cfg, today, country, e, new,
                         title=f"Trim {e['entity']} from {e['live_budget']:.0f} to {new:.0f} EUR a day",
                         reason=(f"{e['entity']} spent {e['delivery']:.0%} of its daily budget over the last 7 days and earned "
                                 f"{e['shopify_roas_7d']:.2f} EUR of Shopify revenue per EUR, below the "
                                 f"{rules['min_shopify_roas_to_raise']} floor. A {rules['auto_lower_max_pct']}% trim is within "
                                 f"the rules the system may apply alone."),
                         evidence={"delivery_7d": round(e["delivery"], 3), "shopify_roas_7d": round(e["shopify_roas_7d"], 2)})
                row["action"] = f"trim {e['entity']}"

        # 4. Under the month: raise only where it is safe and earns.
        if row["projected"] < budget * (1 - tol) and row["action"] == "on plan":
            row["action"] = _maybe_raise(queue, cfg, today, row, frozen)
        report.append(row)
    return report


def _hand_raises(data, cfg, today):
    """Budget raises made in the platforms this month that broke the rules or came from someone who cannot approve."""
    first, _ = month_bounds(today - dt.timedelta(days=1))
    out = {}
    for c in data.changes:
        if c["field"] != "daily_budget" or c["date"] < first.isoformat():
            continue
        old, new = float(c["old"]), float(c["new"])
        step = (new - old) / old * 100
        user = c["user"].split("@")[0]
        if new > old and (step > cfg["rules"]["max_raise_pct"] or user not in cfg["approvers"]["money"]):
            out[c["entity"]] = {"date": c["date"], "old": old, "new": new, "user": c["user"], "step": round(step)}
    return out


def _maybe_raise(queue, cfg, today, row, frozen):
    rules = cfg["rules"]
    country = row["country"]
    if country in frozen:
        return f"under plan, raises frozen: {frozen[country]}"
    best = None
    for e in row["entities"]:
        if e["delivery"] < 0.9:
            continue  # not spending what it has, more budget will not help
        if e["shopify_roas_7d"] >= rules["min_shopify_roas_to_raise"] and (best is None or e["shopify_roas_7d"] > best["shopify_roas_7d"]):
            best = e
    if not best:
        return "under plan, no campaign both spends its budget and earns enough on Shopify"
    gap = (row["budget"] - row["projected"]) / max(row["remaining_days"], 1)
    new = round(min(best["live_budget"] * (1 + rules["max_raise_pct"] / 100), best["live_budget"] + gap), 2)
    _propose(queue, cfg, today, country, best, new,
             title=f"Raise {best['entity']} from {best['live_budget']:.0f} to {new:.0f} EUR a day",
             reason=(f"{country} is on course for {row['projected']:,.0f} EUR against {row['budget']:,.0f}, and {best['entity']} "
                     f"spends its budget and earns {best['shopify_roas_7d']:.2f} EUR of Shopify revenue per EUR."),
             evidence={"shopify_roas_7d": round(best["shopify_roas_7d"], 2), "projected": row["projected"]})
    return f"raise {best['entity']}"


def _propose(queue, cfg, today, country, e, new, title, reason, evidence, kind_note=""):
    rules = cfg["rules"]
    old = e["live_budget"]
    broken = check_budget_change(old, new, today, queue.last_raise(e["platform"], e["entity"]), rules)
    tier = tier_for(old, new, rules)
    if kind_note == "revert":
        tier = "approval"      # undoing someone's change is always a person's call
    evidence = {**evidence, "rules_checked": broken or "all passed"}
    if broken:
        title, tier = "BLOCKED by rules: " + title, "manual"
    return queue.propose("budget_change", title, reason, evidence, tier,
                         dedupe_key=f"budget:{e['platform']}:{e['entity']}:{today.isoformat()}",
                         platform=e["platform"], entity=e["entity"], country=country, field="daily_budget",
                         old_value=old, new_value=new)


def month_start_reset(data, queue, api, cfg, today: dt.date):
    """On the first run of a month, every budget goes back to its base plan, so no ramp carries over.

    Lowers back to plan follow the normal tiers. Going back UP to plan is not a ramp, so the +20% step does not
    apply, but it always needs a named approver.
    """
    if today.day != 1:
        return []
    days = calendar.monthrange(today.year, today.month)[1]
    ids = []
    for platform, entity, country in sorted({(r["platform"], r["entity"], r["country"]) for r in data.entity_rows()}):
        live = api.get(platform, entity)
        base = round(data.budgets[country] * data.split[entity.split(" ", 1)[1]] / days, 2)
        if abs(live - base) < 0.5:
            continue
        tier = tier_for(live, base, cfg["rules"]) if base < live else "approval"
        ids.append(queue.propose("budget_change", f"New month: {entity} back to plan, {live:.0f} to {base:.0f} EUR a day",
                                 f"{today:%B} starts from the plan, so nothing ramped last month carries over.",
                                 {"plan_reset": True, "month_budget": data.budgets[country]}, tier,
                                 dedupe_key=f"reset:{entity}:{today.isoformat()}", platform=platform, entity=entity,
                                 country=country, field="daily_budget", old_value=live, new_value=base))
    return ids
