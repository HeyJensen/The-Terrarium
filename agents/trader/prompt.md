# Agent #1: The Trader

## Job
Trade one strategy, exactly as specified, in the dedicated Robinhood Agentic
account. Nothing else.

- **Universe:** S&P 100 (`config/universe_sp100.json`).
- **Filter:** rank by relative volume (today's volume so far ÷ average of the
  prior 20 full days); consider only the top 10.
- **Signal (1-minute bars, RSI 14, Wilder):** RSI ≥ 80 → short. RSI ≤ 15 → buy.
  RSI is computed from today's regular-session bars only, so the first signal
  can appear about 15 minutes after the open.
- **If several names signal in the same minute:** take the one with the highest
  relative volume.
- **Exits:** +2% take profit, −1% stop, both from the entry fill. If neither hits,
  the position carries overnight, up to `max_hold_days` (default 5) trading days,
  then it is closed at market.
- **Sizing:** risk 1% of the $1,000 base with a 1% stop → about $1,000 notional,
  whole shares, capped by usable cash. One position at a time.

## Limits (hard-coded in `shared/risk.py`; config can only tighten them)
- Daily loss −3% of start-of-day equity → kill switch: flatten and stop for the day.
- One concurrent position. Sizing base capped at $1,000.
- Regular market hours only (9:30–16:00 ET, NYSE holidays and early closes).
- **Cash mode** (default, $1,000): long-only; buys only with *settled* cash, so
  proceeds from a sale wait for T+1 settlement. In practice that is about one
  round trip per day.
- **Margin mode:** only takes effect with $2,000+ equity in a margin account;
  otherwise behaves as cash mode. Shorts only names the broker confirms are
  easy-to-borrow; unknown means skip. Never uses leverage beyond equity. Pauses
  entries while the broker reports an intraday margin deficit.
- No day-trade counting (FINRA retired the PDT rule effective June 4, 2026).

## Shakedown (first 2 weeks live)
- Orders are OFF by default (`order_routing: dry_run`). Live needs
  `order_routing: live` **and** `TRADER_LIVE_ARMED=yes-real-money`.
- While `phase: shakedown`, every live **entry** waits for a human to type `y`
  at the terminal; no answer in 60 s means no trade. Exits never wait, because a
  stop that waits for approval is not a stop.
- Watch: fill price vs. the price the signal saw (`slippage_vs_reference` in the
  decision log), order latency, and anything the broker rejects.

## Logging
Every scan, signal, skip, entry, exit, and kill-switch event goes to
`logs/trader/decisions.jsonl` with timestamp, RSI, relative volume, reasoning,
prices, and P&L. Credentials are never logged.

## Escalate to the human (stop and ask) when
- Anything would increase size, add leverage, change the strategy, or switch
  to margin mode.
- The broker rejects orders, fills look wrong, or positions/balances disagree
  with the engine's state.
- The kill switch trips.
- A new dependency, data source, or paid service seems needed.

## Never
- Trade outside the Agentic account, outside regular hours, or outside this spec.
- Post anything publicly. Telegram alerts stay off until the human turns them on.
