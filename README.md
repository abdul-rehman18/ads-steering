# Ads steering: one system for Google Ads and Meta Ads, judged on Shopify revenue

Built as a working slice for a DTC brand selling in 10 EU countries and the US. **All data is mock data** (60 days, 10 Sep to 8 Nov 2026) and the ad platforms are a mock API with the same shape as the real ones.

## What it does
- **Daily steering:** keeps each country on its monthly budget, judged on Shopify revenue, inside hard rules. Never pauses, only lowers. Raises at most +20% per step, 3 days apart (hand edits in the platforms count), never in the last days of a month, and every budget goes back to plan on the first day of a new month. An overspend is spread evenly across a country so no single campaign is starved.
- **Weekly audit:** wasted search spend and negatives, feed disapprovals, tracking problems, creative fatigue, pacing.
- **Root cause:** when a campaign's Shopify revenue collapses, every likely cause is tested and marked SUPPORTED, RULED OUT or CANNOT TELL, with the data that would settle it.
- **Acts safely:** everything goes into one queue with an owner and a deadline. Small lowers may run alone. Everything else needs a named approver, recorded. Each write is a dry run, then applied, then read back, with the old value kept for undo.

## The three planted problems, and what the system does
| Problem | Caught by | What happens |
|---|---|---|
| DE Performance Max revenue falls 82% from 28 Oct | Daily collapse check, then root cause | Feed disapprovals SUPPORTED (3% to 65%, missing GTIN, feed file changed the same day). Budget, tracking, shop and market wide causes RULED OUT. Raises frozen in DE. Feed task for Freja |
| FR Meta counts every purchase twice from 20 Oct | Daily tracking check | Meta ROAS 10.5 against Shopify 3.7. Two pixels on every order. Raises frozen in FR. Tracking task for Jonas |
| NL Prospecting raised by hand from 292 to 700 on 1 Nov | Pacing and the platform change history | On course for 36,369 EUR against 25,000. The raise broke the 20% rule and came from someone who cannot approve money, and its Shopify return fell below the floor. Proposal: back to 292, the level that worked, saying the month still ends about 10% over and how the rest would be spread. Mikkel approves, it is applied and read back. A task is opened to talk to Jonas |

## Run it
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q                 # 29 tests
python -m steer.cli demo            # the whole story from a clean state
```
Add `ANTHROPIC_API_KEY` to a `.env` file to let Claude judge search terms and write the Monday summary. Without it, plain rules do both.

Step by step, as it would run each day:
```bash
python -m steer.cli daily                # checks, root cause, steering, small automatic lowers
python -m steer.cli weekly               # audit into the queue
python -m steer.cli queue                # what is open, owner, deadline
python -m steer.cli approve 1 --as mikkel
python -m steer.cli apply 1              # dry run, apply, read back
python -m steer.cli undo 1 --as mikkel   # proposes the undo, which goes through the same path
```

## Where things live
```
config.yaml          owners, approvers, hard rules
steer/data.py        reads the exports
steer/checks.py      data freshness and tracking checks
steer/steering.py    daily pacing and proposals
steer/rules.py       the hard rules, checked at proposal and again at apply
steer/rootcause.py   collapse detection and cause testing
steer/audit.py       weekly audit
steer/queue.py       the queue, approvals, change log (SQLite)
steer/executor.py    dry run, apply, read back, undo
steer/mock_api.py    stand-in for the Google Ads and Meta APIs
steer/llm.py         Claude: search term intent and the Monday summary, both checked by code
data/generate.py     builds the mock data and plants the three problems
```

[FAILURE_MODES.md](FAILURE_MODES.md) has the five ways this breaks in production and the guard for each.
