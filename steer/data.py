"""Load the exports into simple daily tables. No AI here, only reading and adding up."""
from __future__ import annotations

import calendar
import csv
import datetime as dt
import pathlib
from collections import defaultdict

import yaml


def _rows(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class Data:
    def __init__(self, folder: pathlib.Path):
        self.folder = pathlib.Path(folder)
        f = self.folder
        self.orders = _rows(f / "shopify_orders.csv")
        self.google = _rows(f / "google_campaign_daily.csv")
        self.meta = _rows(f / "meta_adset_daily.csv")
        self.meta_ads = _rows(f / "meta_ad_daily.csv")
        self.pixel = _rows(f / "meta_pixel_events.csv")
        self.feed = _rows(f / "merchant_center_feed.csv")
        self.terms = _rows(f / "google_search_terms.csv")
        self.changes = _rows(f / "change_history.csv")
        b = yaml.safe_load((f / "budgets.yaml").read_text())
        self.budgets, self.split = b["monthly"], b["split"]
        for r in self.google + self.meta:
            r["spend"] = float(r["spend"])
            r["daily_budget"] = float(r["daily_budget"])
        for r in self.orders:
            r["total_eur"] = float(r["total_eur"])
            r["date"] = r["created_at"][:10]
        self.last_date = max(r["date"] for r in self.google + self.meta)

    # ---- freshness ---------------------------------------------------------
    def last_date_by_source(self) -> dict:
        return {
            "shopify": max(r["date"] for r in self.orders),
            "google": max(r["date"] for r in self.google),
            "meta": max(r["date"] for r in self.meta),
            "merchant_center": max(r["date"] for r in self.feed),
        }

    # ---- spend -------------------------------------------------------------
    def entity_rows(self):
        """One row per campaign / ad set per day across both platforms."""
        for r in self.google:
            yield {"platform": "google", "entity": r["campaign"], "country": r["country"], "date": r["date"],
                   "spend": r["spend"], "daily_budget": r["daily_budget"], "claimed_value": float(r["conversion_value"]),
                   "claimed_count": float(r["conversions"])}
        for r in self.meta:
            yield {"platform": "meta", "entity": r["adset"], "country": r["country"], "date": r["date"],
                   "spend": r["spend"], "daily_budget": r["daily_budget"], "claimed_value": float(r["purchase_value"]),
                   "claimed_count": float(r["purchases"])}

    def spend_between(self, start: str, end: str, country=None, entity=None) -> float:
        return sum(r["spend"] for r in self.entity_rows()
                   if start <= r["date"] <= end and (country is None or r["country"] == country)
                   and (entity is None or r["entity"] == entity))

    # ---- Shopify revenue (the truth) --------------------------------------
    def shopify_revenue(self, start, end, country=None, utm_source=None, utm_campaign=None) -> float:
        return sum(o["total_eur"] for o in self.orders
                   if start <= o["date"] <= end and (country is None or o["country"] == country)
                   and (utm_source is None or o["utm_source"] == utm_source)
                   and (utm_campaign is None or o["utm_campaign"] == utm_campaign))

    def shopify_orders(self, start, end, country=None, utm_source=None) -> int:
        return sum(1 for o in self.orders
                   if start <= o["date"] <= end and (country is None or o["country"] == country)
                   and (utm_source is None or o["utm_source"] == utm_source))

    def platform_claimed(self, start, end, platform, country=None, entity=None, field="claimed_value") -> float:
        return sum(r[field] for r in self.entity_rows()
                   if r["platform"] == platform and start <= r["date"] <= end
                   and (country is None or r["country"] == country) and (entity is None or r["entity"] == entity))

    # ---- helpers -------------------------------------------------------------
    def daily_series(self, country, metric, start, end, **kw):
        out = defaultdict(float)
        d = dt.date.fromisoformat(start)
        while d <= dt.date.fromisoformat(end):
            s = d.isoformat()
            if metric == "shopify":
                out[s] = self.shopify_revenue(s, s, country=country, **kw)
            d += dt.timedelta(days=1)
        return dict(out)


def month_bounds(day: dt.date):
    first = day.replace(day=1)
    last = day.replace(day=calendar.monthrange(day.year, day.month)[1])
    return first, last


def days_ago(day: str, n: int) -> str:
    return (dt.date.fromisoformat(day) - dt.timedelta(days=n)).isoformat()
