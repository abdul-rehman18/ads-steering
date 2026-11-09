"""Command line.

    python -m steer.cli daily                 # 06:00 CET: checks, root cause, steering, small automatic changes
    python -m steer.cli weekly                # Monday: audit into the queue, summary for Slack
    python -m steer.cli queue                 # what is open, who owns it, when it is due
    python -m steer.cli approve 7 --as mikkel
    python -m steer.cli apply 7
    python -m steer.cli undo 7 --as mikkel
    python -m steer.cli demo                  # the whole story from a clean state
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import shutil
import sys

import yaml
from dotenv import load_dotenv

from steer import audit, executor, llm, rootcause, steering
from steer.checks import freshness, tracking
from steer.data import Data
from steer.mock_api import MockAdsAPI
from steer.queue import Queue

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "mock"
WORK = ROOT / "work"
TODAY = dt.date(2026, 11, 9)   # the mock data runs to 8 Nov, so the morning run is on the 9th
load_dotenv(ROOT / ".env")


def setup(work=WORK, today=TODAY):
    work.mkdir(parents=True, exist_ok=True)
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    data = Data(DATA)
    api = MockAdsAPI(work / "platform_state.json", DATA / "live_platform_state.json")
    q = Queue(work / "state.db", cfg, today)
    q.import_platform_changes(data.changes)
    return cfg, data, api, q


def compute_frozen(cfg, data, today):
    """Worked out fresh every time it is needed, including right before a write."""
    fresh = freshness(data, today, cfg["rules"]["stale_data_hours"])
    frozen = {}
    for src, f in fresh.items():
        if f["stale"]:
            for c in data.budgets:
                frozen.setdefault(c, f"{src} data last updated {f['last_day']}")
    issues = tracking(data, data.last_date)
    for t in issues:
        frozen.setdefault(t["country"], f"{t['platform']} tracking broken")
    collapses = rootcause.find_collapses(data, data.last_date)
    for c in collapses:
        frozen.setdefault(c["country"], f"open root cause on {c['entity']}")
    return frozen, fresh, issues, collapses


def daily(cfg, data, api, q, today=TODAY, out=print):
    out(f"\n=== Daily run {today}, data up to {data.last_date} ===")
    frozen, fresh, issues, collapses = compute_frozen(cfg, data, today)
    out("Data freshness: " + ", ".join(f"{s} {f['last_day']}" + (" STALE" if f["stale"] else "") for s, f in fresh.items()))
    if frozen:
        out("Raises frozen: " + "; ".join(f"{c} ({why})" for c, why in sorted(frozen.items())))

    audit.raise_tracking_tasks(q, issues)
    for c in collapses:
        r = rootcause.investigate(data, c)
        supported = [x["cause"] for x in r["causes"] if x["status"] == "SUPPORTED"]
        q.propose("investigate", f"{c['entity']} Shopify revenue down {r['drop_pct']}% since {r['drop_started']}",
                  "Causes the data supports: " + (", ".join(supported) or "none yet") + ". Full list in the root cause report.",
                  r, "manual", dedupe_key=f"collapse:{c['entity']}:{r['drop_started']}", country=c["country"])

    steering.month_start_reset(data, q, api, cfg, today)
    rows = steering.run(data, q, api, cfg, today, frozen)
    out("\nCountry  Budget   Spent MTD  Projected  Pace  Shopify MER  Action")
    for r in rows:
        out(f"{r['country']:<8} {r['budget']:>7,}  {r['mtd_spend']:>9,}  {r['projected']:>9,}  {r['pace_pct']:>3}%  "
            f"{r['mer'] or 0:>11.2f}  {r['action']}")

    for item in q.items("proposed"):
        if item["tier"] == "auto":
            res = executor.apply(q, api, cfg, item["id"], today, actor="system (auto, within rules)", frozen=frozen)
            out(f"\nAuto change #{item['id']}: {item['title']} -> {res}")
            out(f"   Why: {item['reason']}")
            for e in q.events(item["id"]):
                out(f"   {e['who']}: {e['what']}")
    return collapses


def weekly(cfg, data, api, q, today=TODAY, out=print, claude=None):
    out(f"\n=== Weekly audit, 7 days to {data.last_date} ===")
    a = audit.run(data, q, today, claude, api=api, cfg=cfg)
    out(f"Wasted spend: {a['wasted_spend']['terms']} search terms, {a['wasted_spend']['cost']:.2f} EUR with no sales")
    for n in [i for i in q.items() if i["kind"] == "add_negative"]:
        out(f"   {n['title']}. {n['reason']}")
    for f in a["feed"]:
        out(f"Feed: {f['country']} {f['disapproved']} of {f['submitted']} products disapproved ({f['reason']})")
    for t in a["tracking"]:
        out(f"Tracking: {t['country']} {t['platform']} claims {t['ratio_now']}x Shopify orders, was {t['ratio_before']}x"
            + (f"; {t['orders_sent_by_two_pixels']} orders sent by two pixels" if t.get("orders_sent_by_two_pixels") else ""))
        out(f"   {t['platform'].title()} says ROAS {t['platform_roas_7d']}, Shopify says {t['shopify_roas_7d']}. "
            f"A system steering on platform ROAS would raise {t['country']} here. This one freezes raises instead.")
    for f in a["fatigue"]:
        out(f"Fatigue: {f['ad']} frequency {f['frequency_7d']}, CTR {f['ctr_start']}% to {f['ctr_now']}%")
    for p in a.get("pacing", []):
        out(f"Pacing: {p['country']} on course for {p['projected']:,} EUR against {p['budget']:,} ({p['pace_pct']}%)")
    o = a["overclaim"]
    out(f"Overlap: across the {o['countries']} countries with healthy tracking, Google and Meta together claim "
        f"{o['median_x']}x the paid revenue Shopify recorded (from {o['low_x']}x to {o['high_x']}x). "
        f"Platform ROAS is never added up or used to steer.")

    negs = [i for i in q.items() if i["kind"] == "add_negative"]
    facts = {"n01": ("search spend with no sales this week", f"{a['wasted_spend']['cost']:,.0f} EUR"),
             "n02": ("negatives proposed", str(len(negs)))}
    k = 3
    for f in a["feed"]:
        facts[f"n{k:02d}"] = (f"{f['country']} products disapproved ({f['reason']})", f"{f['disapproved']} of {f['submitted']}"); k += 1
    for t in a["tracking"]:
        facts[f"n{k:02d}"] = (f"{t['country']} {t['platform']} ROAS as the platform reports it, last 7 days", str(t["platform_roas_7d"])); k += 1
        facts[f"n{k:02d}"] = (f"{t['country']} {t['platform']} ROAS by Shopify orders, last 7 days", str(t["shopify_roas_7d"])); k += 1
    for f in a["fatigue"]:
        facts[f"n{k:02d}"] = (f"times a week people see '{f['ad']}'", str(f["frequency_7d"])); k += 1
    for p in a.get("pacing", []):
        facts[f"n{k:02d}"] = (f"{p['country']} projected month spend vs budget", f"{p['projected']:,} vs {p['budget']:,} EUR"); k += 1
    fallback = "This week: " + "; ".join(f"{m} {v}" for m, v in facts.values()) + ". Owners and deadlines are in the queue."
    text, how = llm.weekly_summary(claude, facts, fallback)
    out(f"\nMonday summary for Slack (written by {how}):\n{text}")
    return a


def show_queue(q, out=print):
    out("\n=== Queue ===")
    out(f"{'#':>3}  {'Status':<9} {'Tier':<8} {'Owner':<6} {'Due':<10} Title")
    for i in q.items():
        out(f"{i['id']:>3}  {i['status']:<9} {i['tier']:<8} {i['owner']:<6} {i['deadline']:<10} {i['title']}")


def show_rootcause(data, collapses, out=print):
    for c in collapses:
        r = rootcause.investigate(data, c)
        out(f"\n=== Root cause: {r['entity']} Shopify revenue {r['shopify_per_day_before']:,} to "
            f"{r['shopify_per_day_now']:,} EUR a day (down {r['drop_pct']}%), started {r['drop_started']} ===")
        for cause in r["causes"]:
            out(f"[{cause['status']}] {cause['cause']}\n    {cause['evidence']}")
            if cause["would_confirm"]:
                out(f"    Would settle it: {cause['would_confirm']}")


def demo(out=print):
    work = ROOT / "work" / "demo"
    shutil.rmtree(work, ignore_errors=True)
    cfg, data, api, q = setup(work)
    claude = llm.Claude()
    collapses = daily(cfg, data, api, q, out=out)
    show_rootcause(data, collapses, out=out)
    weekly(cfg, data, api, q, out=out, claude=claude)
    show_queue(q, out=out)

    nl = next(i for i in q.items() if i["entity"] == "NL Prospecting" and i["kind"] == "budget_change")
    out(f"\n=== Approval: #{nl['id']} {nl['title']} ===")
    out(nl["reason"])
    try:
        q.approve(nl["id"], "freja")
    except PermissionError as e:
        out(f"Freja tries to approve: refused. {e}")
    q.approve(nl["id"], "mikkel")
    out(f"Mikkel approves: recorded as {q.get(nl['id'])['approved_by']} at {q.get(nl['id'])['approved_at']}")
    frozen, *_ = compute_frozen(cfg, data, TODAY)
    res = executor.apply(q, api, cfg, nl["id"], TODAY, actor="system", frozen=frozen)
    for e in q.events(nl["id"]):
        out(f"   {e['who']}: {e['what']}")
    out(f"Result: {res}. Live budget now {api.get('meta', 'NL Prospecting')} EUR, old value {nl['old_value']} kept for undo.")

    neg = next(i for i in q.items() if i["kind"] == "add_negative")
    q.approve(neg["id"], "freja")
    res = executor.apply(q, api, cfg, neg["id"], TODAY, actor="system")
    out(f"\nNegative #{neg['id']} approved by Freja -> {res}. {neg['entity']} negatives now: {api.negatives(neg['entity'])}")

    out("\n=== Undo, same day ===")
    uid = executor.propose_undo(q, api, nl["id"], "mikkel")
    q.approve(uid, "mikkel")
    res = executor.apply(q, api, cfg, uid, TODAY, actor="system", frozen=frozen)
    out(f"#{uid} {q.get(uid)['title']} -> {res}. Live budget {api.get('meta', 'NL Prospecting')}. "
        f"#{nl['id']} is now {q.get(nl['id'])['status']}.")
    out("An undo may skip the raise rules only on the day of the original change. From tomorrow it is an ordinary raise.")

    out("\n=== When a write does not stick ===")
    api.drop_next_write = True
    live = api.get("meta", "NL Prospecting")
    rid = q.propose("budget_change", f"Test: lower NL Prospecting to {live * 0.95:.0f}", "demo of a write the API accepts but drops",
                    {}, "auto", dedupe_key="demo:drop", platform="meta", entity="NL Prospecting", country="NL",
                    field="daily_budget", old_value=live, new_value=round(live * 0.95, 2))
    res = executor.apply(q, api, cfg, rid, TODAY, actor="system", frozen=frozen)
    out(f"API said OK. Read back: {q.get(rid)['note']}. Status: {res}, owner {q.get(rid)['owner']} told.")
    if claude.available:
        u = claude.usage
        out(f"\nClaude: {u['calls']} calls, {u['input_tokens']} tokens in, {u['output_tokens']} out")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["daily", "weekly", "queue", "approve", "reject", "apply", "undo", "demo", "rootcause"])
    p.add_argument("id", nargs="?", type=int)
    p.add_argument("--as", dest="person")
    a = p.parse_args()
    if a.cmd == "demo":
        return demo()
    cfg, data, api, q = setup()
    if a.cmd == "daily":
        daily(cfg, data, api, q)
    elif a.cmd == "weekly":
        weekly(cfg, data, api, q, claude=llm.Claude())
    elif a.cmd == "rootcause":
        show_rootcause(data, rootcause.find_collapses(data, data.last_date))
    elif a.cmd == "queue":
        show_queue(q)
    elif a.cmd == "approve":
        q.approve(a.id, a.person)
        print(f"#{a.id} approved by {a.person}")
    elif a.cmd == "reject":
        q.reject(a.id, a.person)
        print(f"#{a.id} rejected by {a.person}")
    elif a.cmd == "apply":
        frozen, *_ = compute_frozen(cfg, data, TODAY)
        print(executor.apply(q, api, cfg, a.id, TODAY, frozen=frozen))
        for e in q.events(a.id):
            print(f"   {e['who']}: {e['what']}")
    elif a.cmd == "undo":
        uid = executor.propose_undo(q, api, a.id, a.person)
        print(f"Undo proposed as #{uid}. Approve it, then apply it.")


if __name__ == "__main__":
    sys.exit(main())
