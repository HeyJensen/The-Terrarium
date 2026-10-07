# Sharing the Trader: how others run it, and the risks

## For someone who downloads it

1. Install Python 3.11 or newer. Nothing else to install.
2. `python terrarium.py`
   - The first run each day loads a year of daily prices for the S&P 100 from
     Yahoo Finance (about 2 minutes).
   - From 9:30 ET it scans every minute and redraws a console showing the top
     10 stocks by relative volume and, for each, whether every requirement is
     met: RSI, 50/200 EMA trend, fresh data, side allowed (buy-only on a cash
     account), and no trade already open. A row reads `READY → BUY` when all
     pass.
3. Orders are **simulated**. Entries are limit orders at the bid, cancelled if
   not filled in 10 seconds. One trade at a time. Exits (stop and trailing
   stop) are market orders, because a stop that might not fill isn't a stop.
4. Live Robinhood trading is not wired yet (see robinhood-oauth-setup.md).
   When it is, each person connects **their own** Robinhood Agentic account
   through Robinhood's browser login. No credentials are ever shared.

## What the website shows

Every strategy signal (RSI + trend + fresh data) goes into a signal ledger,
scored as if taken at the bid with $1,000, with no one-trade limit. People
compare their bot's trades against it. The shape is in website/README.md;
`--report` publishes it to the dashboard API.

## Why a bot's trades won't match the site exactly

- **One-trade limit.** The site scores every signal; a bot holds one.
- **Bid fills.** A buy at the bid fills only if someone sells to it within
  10 seconds. On a falling stock it often fills; on a bouncing one it often
  doesn't. The dry run *assumes* the fill (setting `passive_fills: assume` in
  the simulator), so dry-run results are optimistic until the shakedown
  measures the real fill rate.
- **Timing and data.** Each person's Yahoo data arrives at slightly different
  times, and their relative-volume ranking refreshes in rotation, so two bots
  can see different top 10s in the same minute.
- **Account differences.** Cash accounts can't short and must wait for
  settled cash (about one round trip a day). Size depends on their balance.

## Note on the "buy and short at the bid" rule

For a **buy**, a limit at the bid is a passive order: you wait for a seller,
you save the spread, and some orders never fill. For a **short**, selling at
the bid crosses the spread and fills almost like a market order. That's what
the code does, as specified.

## Risks of distributing it

These are flags, not legal advice. Talk to a securities lawyer before
charging for any of it.

- **Investment advice.** Selling access to trade signals, alerts, or a bot
  that trades on them (the Skool Caretaker tier, paid Telegram alerts) can
  count as giving investment advice, which may require registering as an
  investment adviser. The publisher's exemption covers impersonal,
  general-audience content, but personalized or automatic execution weakens
  that.
- **Performance claims.** Showing a P&L that assumes every signal fills at the
  bid, with no one-trade limit, overstates what a real account gets. Label it
  hypothetical, and show real-account results separately.
- **Yahoo data terms.** Yahoo's data is for personal use. Each person pulling
  their own data for their own account is the safer pattern. The site
  republishing Yahoo-derived prices, especially behind a paywall, likely needs
  a licensed data source.
- **Real money in other people's hands.** Bugs, Yahoo outages, or a computer
  going to sleep mid-trade can lose money. Stops live in the program, not at
  the broker. Keep dry run the default, require a deliberate arming step for
  live orders (already the case), and ship with a clear disclaimer (the
  launcher prints one).
- **License.** There is no license file yet. Without one, others can't legally
  reuse the code. Pick one (for example MIT, or a source-available license if
  you want to keep commercial rights) before publishing.
