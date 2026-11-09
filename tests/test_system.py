import datetime as dt
import json
import pathlib

import pytest
import yaml

from steer import audit, executor, llm, rootcause, steering
from steer.checks import freshness, tracking
from steer.data import Data
from steer.mock_api import MockAdsAPI
from steer.queue import Queue
from steer.rules import check_budget_change, tier_for

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = Data(ROOT / "data" / "mock")
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
RULES = CFG["rules"]
TODAY = dt.date(2026, 11, 9)


@pytest.fixture
def env(tmp_path):
    api = MockAdsAPI(tmp_path / "state.json", ROOT / "data" / "mock" / "live_platform_state.json")
    q = Queue(tmp_path / "q.db", CFG, TODAY)
    return api, q


def frozen_today():
    f = {t["country"]: "tracking" for t in tracking(DATA, DATA.last_date)}
    for c in rootcause.find_collapses(DATA, DATA.last_date):
        f.setdefault(c["country"], "root cause")
    return f


# ---- hard rules ------------------------------------------------------------------------------
def test_never_pause():
    assert any("pause" in r for r in check_budget_change(100, 0, TODAY, None, RULES))


def test_raise_capped_at_20_percent():
    assert check_budget_change(100, 120, TODAY, None, RULES) == []
    assert any("20%" in r for r in check_budget_change(100, 121, TODAY, None, RULES))


def test_raises_three_days_apart():
    assert any("day(s) ago" in r for r in check_budget_change(100, 110, TODAY, "2026-11-07", RULES))
    assert check_budget_change(100, 110, TODAY, "2026-11-06", RULES) == []


def test_no_raise_into_next_month():
    assert any("next month" in r for r in check_budget_change(100, 110, dt.date(2026, 11, 28), None, RULES))
    assert check_budget_change(100, 90, dt.date(2026, 11, 28), None, RULES) == []   # lowering is fine


def test_only_small_lowers_run_alone():
    assert tier_for(100, 92, RULES) == "auto"
    assert tier_for(100, 80, RULES) == "approval"
    assert tier_for(100, 105, RULES) == "approval"


# ---- the three planted problems --------------------------------------------------------------
def test_nl_hand_raise_is_put_back_to_the_level_that_worked(env):
    api, q = env
    q.import_platform_changes(DATA.changes)
    steering.run(DATA, q, api, CFG, TODAY, frozen_today())
    nl = next(i for i in q.items() if i["entity"] == "NL Prospecting" and i["kind"] == "budget_change")
    ev = json.loads(nl["evidence"])
    assert nl["new_value"] == 292.0 and nl["tier"] == "approval"
    assert "+140%" in nl["reason"] and "cannot approve money" in nl["reason"]
    assert ev["projected_after_revert"] > 25000 and "every NL budget" in nl["reason"]
    assert any(i["kind"] == "investigate" and "Jonas" in i["title"] for i in q.items())


def test_after_the_revert_the_rest_of_the_overspend_is_spread_evenly(env):
    api, q = env
    api.update_budget("meta", "NL Prospecting", 292.0)       # the revert has been applied
    steering.run(DATA, q, api, CFG, dt.date(2026, 11, 10), {})
    nl = [i for i in q.items() if i["country"] == "NL" and i["kind"] == "budget_change"]
    assert len(nl) == 4
    cuts = {round(1 - i["new_value"] / i["old_value"], 2) for i in nl}
    assert len(cuts) == 1 and 0.05 < cuts.pop() < 0.25


def test_auto_trim_only_for_a_campaign_that_overspends_and_earns_too_little(env):
    api, q = env
    steering.run(DATA, q, api, CFG, TODAY, frozen_today())
    autos = [i for i in q.items() if i["tier"] == "auto"]
    assert [a["entity"] for a in autos] == ["US Prospecting"]
    assert "below the 2.5 floor" in autos[0]["reason"]
    assert not [i for i in q.items() if i["entity"] == "US Retargeting"]


def test_catches_fr_double_pixel_and_freezes_raises():
    t = next(x for x in tracking(DATA, DATA.last_date) if x["country"] == "FR")
    assert t["platform"] == "meta" and t["ratio_now"] / t["ratio_before"] > 1.8
    assert t["orders_sent_by_two_pixels"] == t["orders_checked"]
    assert t["platform_roas_7d"] > 2 * t["shopify_roas_7d"]
    assert "FR" in frozen_today()
    assert [x for x in tracking(DATA, DATA.last_date) if x["country"] != "FR"] == []   # no false alarms


def test_root_cause_for_de_pmax_lists_all_causes_and_supports_feed():
    c = next(x for x in rootcause.find_collapses(DATA, DATA.last_date) if x["entity"] == "DE PMax")
    r = rootcause.investigate(DATA, c)
    status = {x["cause"]: x["status"] for x in r["causes"]}
    assert status["Products disapproved in Merchant Center"] == "SUPPORTED"
    assert status["Held back by its budget"] == "RULED OUT"
    assert status["Tracking broke"] == "RULED OUT"
    assert status["Seasonal or market wide drop"] == "RULED OUT"
    assert list(status.values()).count("CANNOT TELL") == 2
    assert r["drop_started"] == "2026-10-28"
    assert len(rootcause.find_collapses(DATA, DATA.last_date)) == 1   # only the planted one


def test_no_raises_while_frozen(env):
    api, q = env
    rows = steering.run(DATA, q, api, CFG, TODAY, frozen_today())
    de = next(r for r in rows if r["country"] == "DE")
    assert de["pace_pct"] < 80 and "frozen" in de["action"]
    assert not [i for i in q.items() if i["country"] == "DE" and i["kind"] == "budget_change"]


def test_stale_data_is_detected():
    assert freshness(DATA, dt.date(2026, 11, 11), RULES["stale_data_hours"])["shopify"]["stale"]
    assert freshness(DATA, dt.date(2026, 11, 10), RULES["stale_data_hours"])["shopify"]["stale"]   # 9 Nov missing
    assert not freshness(DATA, TODAY, RULES["stale_data_hours"])["shopify"]["stale"]


# ---- the safe write path ------------------------------------------------------------------------
def _nl(api, q):
    steering.run(DATA, q, api, CFG, TODAY, frozen_today())
    return next(i for i in q.items() if i["entity"] == "NL Prospecting" and i["kind"] == "budget_change")


def test_money_needs_a_named_approver(env):
    api, q = env
    nl = _nl(api, q)
    with pytest.raises(PermissionError):
        executor.apply(q, api, CFG, nl["id"], TODAY)
    with pytest.raises(PermissionError):
        q.approve(nl["id"], "freja")
    q.approve(nl["id"], "mikkel")
    assert q.get(nl["id"])["approved_by"] == "mikkel"


def test_apply_reads_back_and_undo_restores(env):
    api, q = env
    nl = _nl(api, q)
    q.approve(nl["id"], "mikkel")
    assert executor.apply(q, api, CFG, nl["id"], TODAY) == "verified"
    assert api.get("meta", "NL Prospecting") == pytest.approx(nl["new_value"], abs=0.01)
    uid = executor.propose_undo(q, api, nl["id"], "mikkel")
    q.approve(uid, "mikkel")
    assert executor.apply(q, api, CFG, uid, TODAY) == "verified"
    assert api.get("meta", "NL Prospecting") == 700.0
    assert q.get(nl["id"])["status"] == "undone"


def test_change_made_by_hand_in_the_meantime_is_not_overwritten(env):
    api, q = env
    nl = _nl(api, q)
    q.approve(nl["id"], "mikkel")
    api.update_budget("meta", "NL Prospecting", 500)        # someone edits in Ads Manager
    assert executor.apply(q, api, CFG, nl["id"], TODAY) == "stale"
    assert api.get("meta", "NL Prospecting") == 500


def test_write_that_does_not_stick_is_caught(env):
    api, q = env
    nl = _nl(api, q)
    q.approve(nl["id"], "mikkel")
    api.drop_next_write = True
    assert executor.apply(q, api, CFG, nl["id"], TODAY) == "failed"
    assert "expected" in q.get(nl["id"])["note"]


def test_rules_rechecked_at_apply_time(env):
    api, q = env
    qid = q.propose("budget_change", "raise", "test", {}, "approval", "t1", platform="meta", entity="SE Prospecting",
                    country="SE", field="daily_budget", old_value=210.0, new_value=260.0)
    q.approve(qid, "mikkel")
    assert executor.apply(q, api, CFG, qid, TODAY) == "failed"
    assert "20%" in q.get(qid)["note"]
    assert api.get("meta", "SE Prospecting") == 210.0


def test_same_problem_is_queued_once(env):
    api, q = env
    steering.run(DATA, q, api, CFG, TODAY, frozen_today())
    n = len(q.items())
    steering.run(DATA, q, api, CFG, TODAY, frozen_today())
    assert len(q.items()) == n


# ---- Claude on a short lead ---------------------------------------------------------------------
def test_negatives_never_contain_a_product_word(env):
    api, q = env
    bad = llm.Claude(call=lambda s, u: json.dumps({"terms": [
        {"term": t, "unrelated": True, "negative": t, "why": "whole term"} for t in json.loads(u)["terms"]]}))
    audit.run(DATA, q, TODAY, bad)
    negs = [json.loads(i["evidence"])["negative"] for i in q.items() if i["kind"] == "add_negative"]
    assert negs and not any(w in n.lower() for n in negs for w in llm.PROTECTED)


def test_claude_typing_numbers_in_summary_falls_back():
    facts = {"n01": ("wasted", "1,054 EUR")}
    c = llm.Claude(call=lambda s, u: json.dumps({"text": "We wasted about 1000 EUR."}))
    text, how = llm.weekly_summary(c, facts, "fallback")
    assert text == "fallback" and "failed" in how
    c = llm.Claude(call=lambda s, u: json.dumps({"text": "We wasted {{n01}} on searches."}))
    text, how = llm.weekly_summary(c, facts, "fallback")
    assert text == "We wasted 1,054 EUR on searches." and how.startswith("claude")


def test_full_demo_runs():
    from steer import cli
    lines = []
    cli.demo(out=lines.append)
    text = "\n".join(lines)
    for must in ["Auto change", "[SUPPORTED] Products disapproved", "Tracking: FR", "Pacing: NL",
                 "Freja tries to approve: refused", "read back", "is now undone", "expected 665.0", "-> verified"]:
        assert must in text, must


# ---- fixes from review ------------------------------------------------------------------------------
def test_buyer_words_are_never_negatives(env):
    api, q = env
    bad = llm.Claude(call=lambda s, u: json.dumps({"terms": [
        {"term": t, "unrelated": True, "negative": "fabrication" if "fabrication" in t else None, "why": "x"}
        for t in json.loads(u)["terms"]]}))
    audit.run(DATA, q, TODAY, bad)
    negs = [json.loads(i["evidence"])["negative"] for i in q.items() if i["kind"] == "add_negative"]
    assert "fabrication" not in negs and "diy" in negs


def test_undo_next_day_follows_the_ordinary_rules(env):
    api, q = env
    nl = _nl(api, q)
    q.approve(nl["id"], "mikkel")
    executor.apply(q, api, CFG, nl["id"], TODAY)
    uid = executor.propose_undo(q, api, nl["id"], "mikkel")
    q.approve(uid, "mikkel")
    assert executor.apply(q, api, CFG, uid, TODAY + dt.timedelta(days=1)) == "failed"
    assert "20%" in q.get(uid)["note"]


def test_hand_raises_in_the_platform_count_for_the_three_day_rule(env):
    api, q = env
    q.import_platform_changes([{"date": "2026-11-08", "platform": "meta", "entity": "SE Prospecting",
                                "field": "daily_budget", "old": "200", "new": "210", "user": "someone"}])
    assert q.last_raise("meta", "SE Prospecting") == "2026-11-08"
    qid = q.propose("budget_change", "raise", "t", {}, "approval", "t3", platform="meta", entity="SE Prospecting",
                    country="SE", field="daily_budget", old_value=210.0, new_value=230.0)
    q.approve(qid, "mikkel")
    assert executor.apply(q, api, CFG, qid, TODAY) == "failed"
    assert "day(s) ago" in q.get(qid)["note"]


def test_freeze_is_checked_again_at_apply_time(env):
    api, q = env
    qid = q.propose("budget_change", "raise", "t", {}, "approval", "t4", platform="meta", entity="FR Prospecting",
                    country="FR", field="daily_budget", old_value=525.0, new_value=600.0)
    q.approve(qid, "mikkel")
    assert executor.apply(q, api, CFG, qid, TODAY, frozen={"FR": "meta tracking broken"}) == "failed"
    assert "frozen" in q.get(qid)["note"]


def test_new_month_resets_budgets_to_plan(env):
    api, q = env
    api.update_budget("meta", "NL Prospecting", 292.0)
    ids = steering.month_start_reset(DATA, q, api, CFG, dt.date(2026, 12, 1))
    nl = [q.get(i) for i in ids if q.get(i)["entity"] == "NL Prospecting"]
    assert nl and round(nl[0]["new_value"]) == round(25000 * 0.35 / 31)
    assert steering.month_start_reset(DATA, q, api, CFG, dt.date(2026, 12, 2)) == []


def test_negative_can_be_undone_and_is_logged(env):
    api, q = env
    audit.run(DATA, q, TODAY)
    neg = next(i for i in q.items() if i["kind"] == "add_negative")
    q.approve(neg["id"], "freja")
    assert executor.apply(q, api, CFG, neg["id"], TODAY) == "verified"
    uid = executor.propose_undo(q, api, neg["id"], "freja")
    q.approve(uid, "freja")
    assert executor.apply(q, api, CFG, uid, TODAY) == "verified"
    assert api.negatives(neg["entity"]) == [] and q.get(neg["id"])["status"] == "undone"
    assert q.db.execute("SELECT COUNT(*) FROM change_log WHERE field LIKE 'negative:%'").fetchone()[0] == 2


def test_only_the_right_people_can_reject_or_close(env):
    api, q = env
    nl = _nl(api, q)
    with pytest.raises(PermissionError):
        q.reject(nl["id"], "jonas")
    task = q.propose("fix_feed", "fix", "t", {}, "manual", "t5", country="DE")
    with pytest.raises(PermissionError):
        q.mark_done(task, "jonas")
    q.mark_done(task, "freja")
    assert q.get(task)["status"] == "done"


def test_overlap_is_measured_only_where_tracking_is_healthy(env):
    api, q = env
    a = audit.run(DATA, q, TODAY)
    assert a["overclaim"]["countries"] == 10 and a["overclaim"]["high_x"] < 1.5


def test_presenter_mode_runs_and_shows_all_parts():
    from steer import cli
    lines = []
    cli.show(out=lines.append, pause=lambda: None)
    text = "\n".join(lines)
    for must in ["PROBLEM 1", "PROBLEM 2", "PROBLEM 3", "REFUSED", "read back", "caught on read back", "ONE QUEUE"]:
        assert must in text, must


def test_names_given_to_claude_hold_no_digits():
    from steer.cli import _no_digits
    assert _no_digits("SE Prospecting ad 1 (bear video)") == "SE Prospecting (bear video)"
