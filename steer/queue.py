"""One queue for everything the system wants to do. Every item has an owner and a deadline.

Statuses:
  proposed   waiting for its owner or an approver
  approved   a named person said yes (recorded)
  rejected   a named person said no (recorded)
  applied    written to the platform, not yet confirmed
  verified   read back from the platform and it matches
  failed     dry run refused it, or the read back did not match
  stale      the live value changed since the proposal was made, so it was not applied
  done       a manual task its owner marked as done
  undone     a later change put the old value back
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  platform TEXT, entity TEXT, country TEXT, field TEXT,
  old_value REAL, new_value REAL,
  title TEXT NOT NULL,
  reason TEXT NOT NULL,
  evidence TEXT NOT NULL,
  owner TEXT NOT NULL,
  deadline TEXT NOT NULL,
  tier TEXT NOT NULL,                  -- auto | approval | manual
  status TEXT NOT NULL DEFAULT 'proposed',
  dedupe_key TEXT UNIQUE,              -- the same problem is never queued twice
  approved_by TEXT, approved_at TEXT,
  dry_run TEXT, applied_at TEXT, readback REAL, verified_at TEXT,
  undo_of INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS change_log (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL, queue_id INTEGER NOT NULL,
  platform TEXT, entity TEXT, field TEXT,
  old_value REAL, new_value REAL, readback REAL, ok INTEGER,
  direction TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, queue_id INTEGER, who TEXT, what TEXT
);
"""


class Queue:
    def __init__(self, path: pathlib.Path, config: dict, today: dt.date):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.config = config
        self.today = today

    # ---- writing -----------------------------------------------------------
    def propose(self, kind, title, reason, evidence: dict, tier, dedupe_key, platform=None, entity=None,
                country=None, field=None, old_value=None, new_value=None, undo_of=None):
        """Add an item. Returns its id, or the existing id if the same problem is already open."""
        row = self.db.execute("SELECT id, status FROM queue WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        if row:
            return row["id"]
        own = self.config["owners"][kind]
        deadline = (self.today + dt.timedelta(days=own["deadline_days"])).isoformat()
        cur = self.db.execute(
            """INSERT INTO queue (created_at, kind, platform, entity, country, field, old_value, new_value, title, reason,
               evidence, owner, deadline, tier, dedupe_key, undo_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.today.isoformat(), kind, platform, entity, country, field, old_value, new_value, title, reason,
             json.dumps(evidence, default=str), own["owner"], deadline, tier, dedupe_key, undo_of))
        self.db.commit()
        self.log(cur.lastrowid, "system", f"proposed ({tier})")
        return cur.lastrowid

    def set(self, qid, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE queue SET {cols} WHERE id=?", (*fields.values(), qid))
        self.db.commit()

    def log(self, qid, who, what):
        self.db.execute("INSERT INTO events (at, queue_id, who, what) VALUES (?,?,?,?)",
                        (dt.datetime.now().isoformat(timespec="seconds"), qid, who, what))
        self.db.commit()

    # ---- decisions -----------------------------------------------------------
    def approve(self, qid, person):
        item = self.get(qid)
        if item["status"] != "proposed":
            raise PermissionError(f"Item {qid} is {item['status']}, only proposed items can be approved")
        money = item["kind"] == "budget_change"
        allowed = self.config["approvers"]["money" if money else "non_money"]
        if person not in allowed:
            self.log(qid, person, "approval refused, not an approver for this kind")
            raise PermissionError(f"{person} may not approve {'money' if money else 'this'} changes. "
                                  f"Allowed: {', '.join(allowed)}")
        now = dt.datetime.now().isoformat(timespec="seconds")
        self.set(qid, status="approved", approved_by=person, approved_at=now)
        self.log(qid, person, "approved")

    def reject(self, qid, person, why=""):
        item = self.get(qid)
        money = item["kind"] == "budget_change"
        if person not in self.config["approvers"]["money" if money else "non_money"]:
            raise PermissionError(f"{person} may not reject this item")
        self.set(qid, status="rejected", approved_by=person, note=why)
        self.log(qid, person, f"rejected {why}".strip())

    def mark_done(self, qid, person):
        item = self.get(qid)
        if item["tier"] != "manual":
            raise ValueError("Only manual tasks are marked done. Platform changes are done when read back")
        if person != item["owner"] and person not in self.config["approvers"]["money"]:
            raise PermissionError(f"Only {item['owner']} or an approver can close this task")
        self.set(qid, status="done")
        self.log(qid, person, "marked done")

    def import_platform_changes(self, changes):
        """Budget raises made by hand in the platforms count for the 3 day rule too."""
        for c in changes:
            if c["field"] != "daily_budget" or float(c["new"]) <= float(c["old"]):
                continue
            plat = {"meta": "meta", "google": "google"}.get(c["platform"])
            seen = self.db.execute("SELECT 1 FROM change_log WHERE queue_id=0 AND entity=? AND at=?",
                                   (c["entity"], c["date"])).fetchone()
            if plat and not seen:
                self.db.execute("""INSERT INTO change_log (at, queue_id, platform, entity, field, old_value, new_value,
                                   ok, direction) VALUES (?,?,?,?,?,?,?,1,'raise')""",
                                (c["date"], 0, plat, c["entity"], "daily_budget", float(c["old"]), float(c["new"])))
        self.db.commit()

    # ---- reading -------------------------------------------------------------
    def get(self, qid):
        return self.db.execute("SELECT * FROM queue WHERE id=?", (qid,)).fetchone()

    def items(self, status=None):
        q = "SELECT * FROM queue" + (" WHERE status=?" if status else "") + " ORDER BY id"
        return self.db.execute(q, (status,) if status else ()).fetchall()

    def last_raise(self, platform, entity):
        row = self.db.execute("""SELECT at FROM change_log WHERE platform=? AND entity=? AND direction='raise' AND ok=1
                                 ORDER BY at DESC LIMIT 1""", (platform, entity)).fetchone()
        return row["at"][:10] if row else None

    def events(self, qid):
        return self.db.execute("SELECT * FROM events WHERE queue_id=? ORDER BY id", (qid,)).fetchall()
