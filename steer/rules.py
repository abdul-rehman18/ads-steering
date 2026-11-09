"""The hard rules. Checked when a change is proposed AND again right before it is applied."""
from __future__ import annotations

import datetime as dt

from steer.data import month_bounds


def check_budget_change(old: float, new: float, today: dt.date, last_raise: str | None, rules: dict,
                        is_undo: bool = False) -> list[str]:
    """Return the rules a budget change breaks. An empty list means it is allowed."""
    broken = []
    if new <= 0:
        broken.append("never pause: a budget of zero is a pause")
    if new < rules["min_daily_budget_eur"]:
        broken.append(f"below the floor of {rules['min_daily_budget_eur']} EUR a day")
    if new > old and not is_undo:
        step = (new - old) / old * 100
        if step > rules["max_raise_pct"] + 1e-9:
            broken.append(f"raise of {step:.1f}% is above the {rules['max_raise_pct']}% limit per step")
        if last_raise:
            gap = (today - dt.date.fromisoformat(last_raise)).days
            if gap < rules["min_days_between_raises"]:
                broken.append(f"last raise was {gap} day(s) ago, rules need {rules['min_days_between_raises']}")
        _, last = month_bounds(today)
        if (last - today).days < rules["no_raise_last_days_of_month"]:
            broken.append("no raises in the last days of a month, so a ramp never carries into the next month")
    return broken


def tier_for(old: float, new: float, rules: dict) -> str:
    """Small lowers may run alone. Every raise and every big lower needs a named approver."""
    if new < old and (old - new) / old * 100 <= rules["auto_lower_max_pct"] + 1e-9:
        return "auto"
    return "approval"
