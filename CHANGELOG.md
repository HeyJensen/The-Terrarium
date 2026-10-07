# Changelog: the Terrarium build log

## 2026-10-07: Trader strategy v0.2

- Universe widened from the S&P 100 to the **S&P 500**.
- Short trigger raised to **RSI ≥ 85** (buy trigger stays RSI ≤ 15).
- New **trend filter**: shorts only when the daily 50 EMA is below the 200 EMA,
  buys only when it's above, using a year of daily bars.
- The **+2% take-profit is gone**. At +2% the stop starts trailing 1% behind
  the best price, so a winner can keep running while about +1% is locked in.
- 32 tests. Orders still off.

## 2026-10-07: Day 1, the foundation and the Trader

**Built**
- Repo skeleton: `agents/`, `shared/`, `dashboard/`, `config/`.
- Config with secrets only in environment variables; the logger scrubs any
  secret value before it hits a log file.
- Shared JSON logger and a decision log: every scan, signal, skip, entry,
  exit, and kill-switch event is written with time, RSI, relative volume,
  reasoning, and P&L.
- Dashboard status API (`/api/agents`, `/api/outbox`, `/api/heartbeat`,
  `/api/dashboard`). Agents are discovered from `agents/manifest.json`.
- Agent #1, the Trader: RSI(14) on 1-minute bars across the 10 highest
  relative-volume S&P 100 names. Buy at RSI ≤ 15, short at RSI ≥ 80 (margin
  mode only), +2% target, −1% stop, one position at a time.
- Hard-coded guardrails: −3% daily kill switch, $1,000 sizing cap, regular
  hours only, cash-account settlement tracking, easy-to-borrow check for shorts.
- A replay mode that runs the full engine over recorded bars with a simulated
  broker, plus 26 tests.

**Learned**
- Robinhood's agent access is an MCP server with browser login, not a
  classic API with keys. It trades only in a dedicated Agentic account and is
  stocks-only for now. Shorting there is unconfirmed.
- The PDT rule really is gone (June 4, 2026), but brokers have until October
  2027 to switch over.

**Not live yet.** Orders are off. Next: pick a market-data source and wire
the Robinhood connection, then start the two-week supervised shakedown.
