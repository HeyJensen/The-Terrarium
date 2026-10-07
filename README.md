# The Terrarium: organism

A team of AI agents that each run one money-making stream, reporting to one
dashboard the public Terrarium website will read. Built in phases; Phase 1 is
a single trading agent.

```
organism/
  agents/       one folder per agent (agent.json, prompt.md, run.py) + manifest.json registry
  shared/       config, logging, risk guardrails, market calendar, notifications
  dashboard/    status API the website reads
  config/       settings.json (no secrets) + .env.example (secrets via env vars only)
  docs/         setup guides and spec review
  tests/        unit + replay tests (python -m unittest)
```

Python 3.11+, standard library only. No dependencies to install.

## Run the Trader console

```bash
python terrarium.py      # scans Yahoo every minute, shows each requirement, simulated orders
```

Sharing it with others, and what to watch out for: `docs/sharing-the-trader.md`.

## Quick start

```bash
cp config/.env.example config/.env       # fill in values; the file is git-ignored
python -m unittest                        # all tests
python agents/trader/run.py --task check  # shows limits and whether live orders are possible
python -m dashboard.server                # http://127.0.0.1:8787/api/dashboard

# Dry run on live Yahoo Finance data (no orders reach any broker)
python agents/trader/run.py --task prefetch   # before 9:30 ET: a year of daily bars
python agents/trader/run.py --task trade      # minute loop, Ctrl+C to stop
```

Market data comes from Yahoo Finance; see `docs/market-data-yahoo.md` for its limits.

Phase 2 commerce bots (social, etsy, studio, content) run in draft-only mode;
see `docs/phase2-commerce-bots.md` for how they connect and how to try them.
Review their drafts with `python -m agents.common.approvals list`.

## Safety switches

- `trader.order_routing` is `dry_run` by default. Live orders need it set to
  `live` **and** `TRADER_LIVE_ARMED=yes-real-money` in the environment.
- During `phase: shakedown`, each live entry waits for a typed `y`.
- Hard limits (−3% daily kill switch, one position, 1% risk, $1,000 sizing
  base) are in `shared/risk.py`; settings can tighten but not loosen them.
- Telegram alerts are off. Nothing in this repo publishes anywhere.

## Adding an agent

Create `agents/<name>/` with `agent.json`, `prompt.md`, `run.py` (supporting
`--task` and `--status`), then add one entry to `agents/manifest.json`. The
dashboard picks it up automatically and accepts its status posts:
`{name, status, current_task, tasks_today, pnl, last_updated}`.
