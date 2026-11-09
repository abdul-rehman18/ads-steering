"""Claude, kept on a short lead.

Two jobs, both checked by code:
1. Judge search term intent. Code first picks candidates (no sales, real cost). Claude only says whether
   each one is unrelated to what the brand sells. Code then refuses any negative that touches a protected
   word, or a term Claude did not get from the list.
2. Write the weekly summary. Claude refers to numbers as {{id}} and code fills them in. A digit typed by
   Claude gets the summary rejected, and after two tries a plain template is used.
Without an API key, both fall back to plain rules, so the system still runs.
"""
from __future__ import annotations

import json
import os
import re

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
PLACEHOLDER = re.compile(r"\{\{(n\d{2})\}\}")
PROTECTED = ["plush", "bear", "teddy", "weighted", "sensory", "calming", "stuffed", "whale", "dino", "bunny", "peluche",
             "plüsch", "knuffel", "gift"]
# Most specific first: the first match becomes the negative when Claude is not available.
# Broad words like "free" come last, because "free shipping plush bear" is a buyer.
UNRELATED_HINTS = ["sewing pattern", "schnittmuster", "factory jobs", "jobs", "diy", "tekenen", "drawing",
                   "how to make", "pattern", "kostenlos", "gratuit", "free"]
# Words that look unrelated but can belong to a buyer ("fabrication française" means made in France).
NEVER_NEGATIVE = ["fabrication", "made", "handmade", "shipping", "livraison", "versand"]


class Claude:
    def __init__(self, call=None):
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        self._call = call
        if call is None and os.environ.get("ANTHROPIC_API_KEY"):
            import anthropic
            client = anthropic.Anthropic()

            def _c(system, user):
                m = client.messages.create(model=MODEL, max_tokens=3000, system=system,
                                           messages=[{"role": "user", "content": user}])
                self.usage["input_tokens"] += m.usage.input_tokens
                self.usage["output_tokens"] += m.usage.output_tokens
                return "".join(b.text for b in m.content if b.type == "text")
            self._call = _c

    @property
    def available(self):
        return self._call is not None

    def ask_json(self, system, user):
        self.usage["calls"] += 1
        raw = self._call(system, user).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        a, b = raw.find("{"), raw.rfind("}")
        return json.loads(raw[a:b + 1])


# ---- 1. search term intent ---------------------------------------------------------------

TERMS_SYSTEM = """You review Google search terms for a brand that sells weighted plush toys (bears, whales, dinos, bunnies)
and gift boxes, online in Europe and the US. Terms can be in any European language.
For each term, decide if the person searching could plausibly want to BUY one of these products.
For each unrelated term also give "negative": the shortest words copied from the term that capture the unrelated
intent WITHOUT any product word (so "free plush toy sewing pattern" gives "sewing pattern", not the whole term).
Reply JSON only: {"terms": [{"term": "<exactly as given>", "unrelated": true|false, "negative": "<words or null>", "why": "<short, plain>"}]}
Mark unrelated only when the intent is clearly something else (jobs, free patterns, making it yourself, drawing).
Never use a word a buyer might also type, such as "fabrication" (it also means "made in", as in "fabrication française").
When unsure, mark false."""


def judge_terms(claude: Claude | None, candidates: list[dict]) -> list[dict]:
    """Returns the candidates that should get a negative keyword, with the exact negative and why.

    Guard: the negative must be words copied from the term and must not contain a product word,
    so a negative can never block people searching for what the brand sells.
    """
    names = [c["search_term"] for c in candidates]
    verdicts, source = {}, "rules"
    if claude is not None and claude.available and names:
        try:
            out = claude.ask_json(TERMS_SYSTEM, json.dumps({"terms": names}, ensure_ascii=False))
            for v in out.get("terms", []):
                if v.get("term") in names:            # ignore anything Claude did not get from us
                    verdicts[v["term"]] = v
            source = "Claude"
        except Exception as e:  # a broken reply falls back to rules, it never blocks the audit
            source = f"rules (Claude reply unusable: {e.__class__.__name__})"
    keep = []
    for c in candidates:
        term = c["search_term"]
        low = term.lower()
        hints = [h for h in UNRELATED_HINTS if re.search(rf"\b{re.escape(h)}\b", low)]
        if term in verdicts:
            unrelated, why = bool(verdicts[term].get("unrelated")), verdicts[term].get("why", "")
            negative = (verdicts[term].get("negative") or "").strip()
        else:
            unrelated, why = bool(hints), (f"Contains '{hints[0]}', which signals a different intent" if hints else "")
            negative = hints[0] if hints else ""
        if not unrelated:
            continue
        guard = ""
        bad = (not negative or negative.lower() not in low or any(w in negative.lower() for w in PROTECTED + NEVER_NEGATIVE))
        if bad:
            guard = f" Proposed negative '{negative}' was refused (empty, not in the term, or could match buyers)."
            negative = next((h for h in hints if not any(w in h for w in PROTECTED)), "")
            if not negative:
                continue  # nothing safe to add; the cost still shows in the audit total
            guard += f" Using '{negative}' instead."
        keep.append({**c, "negative": negative, "why": f"{why.rstrip('.')}. Judged by {source}.{guard}"})
    return keep


# ---- 2. weekly summary --------------------------------------------------------------------

SUMMARY_SYSTEM = """You write the Monday ads summary for the two founders of a small online brand. Plain words, short.
Every number must be written ONLY as a placeholder like {{n03}} from the facts given. Never type a digit yourself.
Do not add advice that is not in the facts. Reply JSON only: {"text": "<the summary, 4 to 7 short sentences>"}"""


def check_grounded(text, allowed):
    used = set(PLACEHOLDER.findall(text))
    problems = []
    if used - allowed:
        problems.append(f"unknown facts {sorted(used - allowed)}")
    stray = re.findall(r"\d", PLACEHOLDER.sub("", text))
    if stray:
        problems.append("contains numbers Claude typed itself")
    return problems


def weekly_summary(claude: Claude | None, facts: dict, fallback: str) -> tuple[str, str]:
    """facts: {"n01": ("what it is", "shown as")}. Returns (text, how it was written)."""
    if claude is None or not claude.available:
        return fallback, "template (no Claude key)"
    allowed = set(facts)
    prompt = json.dumps({k: {"means": v[0], "shows_as": v[1]} for k, v in facts.items()}, ensure_ascii=False)
    for attempt in (1, 2):
        try:
            text = claude.ask_json(SUMMARY_SYSTEM, prompt)["text"]
        except Exception:
            continue
        if not check_grounded(text, allowed):
            return PLACEHOLDER.sub(lambda m: facts[m.group(1)][1], text), f"claude (attempt {attempt})"
        prompt += "\n\nYour last reply typed numbers or used unknown ids. Use only {{nNN}} placeholders."
    return fallback, "template (Claude failed the number check twice)"
