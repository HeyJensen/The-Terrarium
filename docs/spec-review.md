# Spec review: regulatory claims and defaults

Checked 2026-10-07.

## Regulatory claims in the spec

| Claim | Verdict |
|---|---|
| FINRA eliminated the pattern-day-trader rule on June 4, 2026, replaced by intraday margin monitoring | **True, with a catch.** FINRA Regulatory Notice 26-10, effective June 4, 2026. But firms may keep the old day-trading rules during a transition through **October 20, 2027**. A third-party article says Robinhood dropped PDT flags when the change took effect; I could not confirm that from Robinhood itself. The engine does no day-trade counting, as specified. If Robinhood ever flags the account, tell Claude. |
| Margin account requires $2,000 minimum equity, "Reg T" | **True on the amount, wrong rule name.** The $2,000 minimum comes from FINRA Rule 4210, not Regulation T (Reg T sets the 50% initial margin). Same practical effect. |
| Repeated intraday margin deficits can mean up to 90-day restrictions | **True** per FINRA's investor guidance on the new intraday margin rules. |
| $1,000 cash account: long-only, T+1 settlement, trading unsettled funds = good-faith violation | **True.** US equities settle T+1 since May 28, 2024. |
| Shorting needs margin + easy-to-borrow | **True in general, but** the Robinhood Agentic account may not support shorting at all (see robinhood-oauth-setup.md). |

Sources: [FINRA: Understanding the New Intraday Margin Requirements](https://www.finra.org/investors/insights/intraday-margin-requirements),
[FINRA Regulatory Notice 26-10](https://www.finra.org/sites/default/files/2026-04/Regulatory-Notice-26-10.pdf),
[ACA Group summary](https://www.acaglobal.com/industry-insights/finra-ends-the-pattern-day-trader-rule/),
[Finder on Robinhood day trading](https://www.finder.com/stock-trading/robinhood-day-trading).

## Strategy changes from Nathan (2026-10-07, second message)

| Change | Was | Now | Note |
|---|---|---|---|
| Universe | S&P 100 | ~~S&P 500~~ → back to **S&P 100** (Nathan, later the same day) | Keeps the relative-volume filter (top 10 names). `config/universe_sp500.json` is still there if wanted. |
| Short trigger | RSI ≥ 80 | **RSI ≥ 85** | Long trigger stays RSI ≤ 15. |
| Trend filter | none | **50 EMA vs 200 EMA on daily bars, 1 year** | Shorts only in a downtrend, longs only in an uptrend. In cash mode only the long side matters. |
| Exit at +2% | sell (take profit) | **stop moves up instead** | Default `trail`: stop trails 1% behind the best price, so about +1% is locked when +2% is reached. Options: `breakeven`, `take_profit`. |

Effects worth knowing: the trend filter removes most RSI ≤ 15 buys during
market selloffs, so expect fewer trades. With no fixed target, winners can
run until the trailing stop or the 5-day max hold.

## Defaults chosen where the spec was silent

- **RSI warm-up:** RSI(14) uses only today's regular-session 1-minute bars, so
  the first possible signal is about 9:45 ET. Avoids yesterday's close-to-open
  gap distorting the first readings.
- **Relative volume:** exactly as written: today's volume *so far* ÷ the
  20-day average *full-day* volume. Early in the day every name reads low, but
  the ranking (top 10) is still meaningful.
- **Several signals at once:** take the highest relative volume.
- **Both target and stop inside one bar:** assume the stop hit (conservative).
- **Gap through a level at the open:** exit at the open price.
- **Trailing stop timing:** each 1-minute bar is checked against the stop as it
  stood when the bar opened, then the stop is moved using that bar's high
  (low for shorts).
- **Day P&L for the −3% kill switch:** measured from the previous session's
  ending equity, so an overnight gap counts.
- **Kill switch:** also flattens an open position (setting
  `kill_switch_flattens_position`).
- **Max hold:** exits at market once 5 trading days have passed since entry.
- **Sizing base is fixed at $1,000** from config, not current equity, so the
  bot never scales itself up. Raising it is blocked in code
  (`shared/risk.py`) until you approve.
- **Supervision:** live entries need a typed `y` during the shakedown; exits
  never wait.

## Things to know before going live

- **Cash mode means roughly one round trip per day.** After a sale the cash is
  unsettled until the next trading day, and the engine only buys with settled
  cash.
- **Stops live in the engine, not at the broker.** If the engine or your
  computer stops, there is no stop protecting the position. Resting broker
  stop orders depend on what the MCP supports (unconfirmed).
- **Market data comes from Yahoo Finance** (Nathan's call). It's free but
  unofficial and personal-use only; see market-data-yahoo.md. It refreshes
  the top 10 names every minute and the rest of the S&P 100 in rotation. Robinhood's MCP doesn't
  document either.
