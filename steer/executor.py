"""Apply a queue item safely: check it is still valid, dry run, apply, read back, keep the old value for undo."""
from __future__ import annotations

import datetime as dt
import json

from steer.mock_api import ApiError
from steer.rules import check_budget_change

TOL = 0.005  # half a cent


def apply(queue, api, cfg, qid, today: dt.date, actor="system", frozen: dict | None = None) -> str:
    """frozen: {country: reason} worked out fresh at the moment of applying, so a raise approved before
    tracking broke is still stopped."""
    item = queue.get(qid)
    if item["kind"] not in ("budget_change", "add_negative", "remove_negative"):
        raise ValueError(f"Item {qid} is a task for {item['owner']}, not a platform change")
    if item["tier"] == "manual":
        raise PermissionError(f"Item {qid} broke a rule when proposed and cannot be applied")
    if item["tier"] == "approval" and item["status"] != "approved":
        raise PermissionError(f"Item {qid} needs approval first (status: {item['status']})")
    if item["tier"] == "auto" and item["status"] not in ("proposed", "approved"):
        raise PermissionError(f"Item {qid} is {item['status']}")
    if item["kind"] in ("add_negative", "remove_negative"):
        return _apply_negative(queue, api, item, today, actor)

    platform, entity, old, new = item["platform"], item["entity"], item["old_value"], item["new_value"]
    rules = cfg["rules"]

    # 1. Has anyone changed it since we proposed? Then our "old" value is wrong and so is the undo.
    live = api.get(platform, entity)
    if abs(live - old) > TOL:
        queue.set(qid, status="stale", note=f"live value is {live}, proposal was made against {old}")
        queue.log(qid, actor, f"not applied, live value changed to {live}")
        return "stale"

    # 2. Every rule again, at the moment of writing.
    ev = json.loads(item["evidence"] or "{}")
    exempt = bool(ev.get("plan_reset")) or _same_day_undo(queue, item, today)
    broken = check_budget_change(old, new, today, queue.last_raise(platform, entity), rules, is_undo=exempt)
    if item["tier"] == "auto" and not (new < old and (old - new) / old * 100 <= rules["auto_lower_max_pct"] + 1e-9):
        broken.append("marked automatic but is not a small lower")
    if new > old and not exempt and frozen and item["country"] in frozen:
        broken.append(f"raises in {item['country']} are frozen: {frozen[item['country']]}")
    if broken:
        queue.set(qid, status="failed", note="; ".join(broken))
        queue.log(qid, actor, "not applied, rules: " + "; ".join(broken))
        return "failed"

    # 3. Dry run.
    try:
        dry = api.update_budget(platform, entity, new, validate_only=True)
    except ApiError as e:
        queue.set(qid, status="failed", dry_run=f"refused: {e}")
        queue.log(qid, actor, f"dry run refused: {e}")
        return "failed"
    queue.set(qid, dry_run=f"platform would store {dry['would_store']}")
    queue.log(qid, actor, f"dry run ok, platform would store {dry['would_store']}")

    # 4. Apply, logging the old value first so it can always be undone.
    now = dt.datetime.now().isoformat(timespec="seconds")
    log_id = queue.db.execute(
        "INSERT INTO change_log (at, queue_id, platform, entity, field, old_value, new_value, direction) VALUES (?,?,?,?,?,?,?,?)",
        (today.isoformat() + now[10:], qid, platform, entity, "daily_budget", old, dry["would_store"],
         "raise" if new > old else "lower")).lastrowid
    queue.db.commit()
    api.update_budget(platform, entity, new)
    queue.set(qid, status="applied", applied_at=today.isoformat() + now[10:])
    queue.log(qid, actor, "applied")

    # 5. Read back. Done means the platform holds the value, not that the API said OK.
    back = api.get(platform, entity)
    ok = abs(back - dry["would_store"]) <= TOL
    queue.db.execute("UPDATE change_log SET readback=?, ok=? WHERE id=?", (back, int(ok), log_id))
    queue.db.commit()
    if ok:
        queue.set(qid, status="verified", readback=back, verified_at=now)
        queue.log(qid, actor, f"read back {back}, matches")
        if item["undo_of"]:
            queue.set(item["undo_of"], status="undone")
            queue.log(item["undo_of"], actor, f"undone by item {qid}")
        return "verified"
    queue.set(qid, status="failed", readback=back, note=f"platform holds {back}, expected {dry['would_store']}")
    queue.log(qid, actor, f"read back {back} does NOT match {dry['would_store']}, flagged for {item['owner']}")
    return "failed"


def _same_day_undo(queue, item, today):
    """An undo may skip the raise rules only on the day the original change was made. After that the
    original has become the new normal, and going back is an ordinary change under the ordinary rules."""
    if not item["undo_of"]:
        return False
    orig = queue.get(item["undo_of"])
    return bool(orig and orig["applied_at"] and orig["applied_at"][:10] == today.isoformat())


def propose_undo(queue, api, qid, person) -> int:
    """Undo goes through the same path: a new proposal, a named approval, dry run, apply, read back."""
    item = queue.get(qid)
    if item["status"] != "verified":
        raise ValueError(f"Only verified changes can be undone (item {qid} is {item['status']})")
    if item["kind"] == "add_negative":
        ev = json.loads(item["evidence"])
        return queue.propose("remove_negative", f"Undo item {qid}: remove negative '{ev['negative']}' from {item['entity']}",
                             f"Requested by {person}.", ev, "approval", dedupe_key=f"undo:{qid}", platform="google",
                             entity=item["entity"], country=item["country"], undo_of=qid)
    if item["kind"] != "budget_change":
        raise ValueError(f"Item {qid} cannot be undone")
    live = api.get(item["platform"], item["entity"])
    return queue.propose("budget_change", f"Undo item {qid}: put {item['entity']} back to {item['old_value']:.2f} EUR",
                         f"Requested by {person}.", {"undo_of": qid, "live_now": live}, "approval",
                         dedupe_key=f"undo:{qid}", platform=item["platform"], entity=item["entity"],
                         country=item["country"], field="daily_budget", old_value=live,
                         new_value=item["old_value"], undo_of=qid)


def _apply_negative(queue, api, item, today, actor):
    qid, entity = item["id"], item["entity"]
    ev = json.loads(item["evidence"])
    kw, match = ev["negative"], ev.get("match_type", "PHRASE")
    removing = item["kind"] == "remove_negative"
    before = api.negatives(entity)
    try:
        if removing:
            api.remove_negative(entity, kw, validate_only=True)
        else:
            api.add_negative(entity, kw, match_type=match, validate_only=True)
    except ApiError as e:
        queue.set(qid, status="failed", dry_run=f"refused: {e}")
        queue.log(qid, actor, f"dry run refused: {e}")
        return "failed"
    queue.set(qid, dry_run="ok")
    queue.log(qid, actor, "dry run ok")
    now = dt.datetime.now().isoformat(timespec="seconds")
    queue.db.execute(
        "INSERT INTO change_log (at, queue_id, platform, entity, field, old_value, new_value, direction) VALUES (?,?,?,?,?,?,?,?)",
        (today.isoformat() + now[10:], qid, "google", entity, f"negative:{kw}", None, None,
         "remove" if removing else "add"))
    queue.db.commit()
    (api.remove_negative if removing else api.add_negative)(entity, kw)
    queue.set(qid, status="applied", applied_at=today.isoformat() + now[10:])
    queue.log(qid, actor, f"applied (negative list had {len(before)} entries)")
    present = kw in api.negatives(entity)
    if present != removing:
        queue.set(qid, status="verified", verified_at=now)
        queue.log(qid, actor, f"read back: '{kw}' is {'gone from' if removing else 'in'} the negative list")
        if item["undo_of"]:
            queue.set(item["undo_of"], status="undone")
        return "verified"
    queue.set(qid, status="failed", note="negative list did not change on read back")
    queue.log(qid, actor, "read back does not match")
    return "failed"
