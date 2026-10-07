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

## Quick start

```bash
cp config/.env.example config/.env       # fill in values; the file is git-ignored
python -m unittest                        # 26 tests
python agents/trader/run.py --task check  # shows limits and whether live orders are possible
python -m dashboard.server                # http://127.0.0.1:8787/api/dashboard
```

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
