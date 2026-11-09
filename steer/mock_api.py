"""A stand-in for the Google Ads and Meta Marketing APIs, with the behaviours that bite in real life.

- validate_only: checks a change and returns what the platform would store, without writing (Google has
  validateOnly, Meta has execution_options=validate_only).
- Money is stored in minor units: Meta in cents, Google in micros rounded to the cent. What you send is not
  always exactly what is stored, which is why the system reads values back.
- Minimum daily budget.
- `drop_next_write` simulates a write the API accepts but that does not stick, the case read back exists for.
State lives in a JSON file so a read back in a later process sees what an earlier process wrote.
"""
from __future__ import annotations

import json
import pathlib
import shutil


class ApiError(Exception):
    pass


class MockAdsAPI:
    MIN_BUDGET = 1.0

    def __init__(self, state_path: pathlib.Path, seed_path: pathlib.Path | None = None):
        self.path = pathlib.Path(state_path)
        if not self.path.exists():
            if seed_path is None:
                raise FileNotFoundError(self.path)
            shutil.copy(seed_path, self.path)
        self.drop_next_write = False
        self.calls = []

    def _load(self):
        return json.loads(self.path.read_text())

    def _save(self, s):
        self.path.write_text(json.dumps(s, indent=2))

    def _find(self, s, platform, entity):
        try:
            return s[platform][entity]
        except KeyError:
            raise ApiError(f"{platform} has no {entity}")

    @staticmethod
    def _store_value(platform, value):
        if platform == "meta":
            return int(round(value * 100)) / 100          # cents
        micros = int(round(value * 1_000_000 / 10_000)) * 10_000
        return micros / 1_000_000                          # micros, cent steps

    # ---- reads ------------------------------------------------------------------
    def get(self, platform, entity, field="daily_budget"):
        self.calls.append(("get", platform, entity))
        return self._find(self._load(), platform, entity)[field]

    def get_status(self, platform, entity):
        return self._find(self._load(), platform, entity)["status"]

    def negatives(self, entity):
        return list(self._find(self._load(), "google", entity).get("negative_keywords", []))

    # ---- writes -----------------------------------------------------------------
    def update_budget(self, platform, entity, value, validate_only=False):
        self.calls.append(("update_budget", platform, entity, value, validate_only))
        s = self._load()
        obj = self._find(s, platform, entity)
        if value < self.MIN_BUDGET:
            raise ApiError(f"Budget {value} is below the platform minimum {self.MIN_BUDGET}")
        stored = self._store_value(platform, value)
        if validate_only:
            return {"would_store": stored, "current": obj["daily_budget"]}
        if self.drop_next_write:
            self.drop_next_write = False
            return {"stored": stored}                       # says OK, writes nothing
        obj["daily_budget"] = stored
        self._save(s)
        return {"stored": stored}

    def add_negative(self, entity, keyword, match_type="PHRASE", validate_only=False):
        self.calls.append(("add_negative", entity, keyword, match_type, validate_only))
        s = self._load()
        obj = self._find(s, "google", entity)
        if len(keyword) > 80:
            raise ApiError("Keyword too long")
        if validate_only:
            return {"would_add": keyword}
        obj.setdefault("negative_keywords", [])
        if keyword not in obj["negative_keywords"]:
            obj["negative_keywords"].append(keyword)
        self._save(s)
        return {"added": keyword}

    def remove_negative(self, entity, keyword, validate_only=False):
        s = self._load()
        obj = self._find(s, "google", entity)
        if keyword not in obj.get("negative_keywords", []):
            raise ApiError(f"'{keyword}' is not a negative on {entity}")
        if validate_only:
            return {"would_remove": keyword}
        obj["negative_keywords"].remove(keyword)
        self._save(s)
        return {"removed": keyword}

    def set_status(self, platform, entity, status, validate_only=False):
        """Exists on the real APIs. The executor refuses to ever call it with a pause."""
        raise ApiError("Status changes are not used by this system")
