# Changelog: the Terrarium build log

## 2026-10-07: The Builder, a bot that grows the Terrarium

- New `agents/builder`: audits every bot against the house rules (prompt.md,
  agent.json, manifest entry, tests) and flags drafts waiting too long for
  approval.
- It ranks money-making bot ideas (Pinterest, Redbubble, Gumroad, KDP,
  newsletter, Fiverr, a bookkeeper) and sends the best ones to the approval
  queue, each flagged with what it would need: an account, public posting,
  or spend.
- Approve an idea and the builder scaffolds the new bot, draft-only and
  switched off until you turn it on.
- A weekly Claude routine runs it, researches fresh free-to-start ideas, and
  opens a draft pull request for review. It never merges or publishes.

## 2026-10-07: The Terrarium talks with Whisper and Fish Audio

- New `website/voice_server.py` runs on your own computer: Whisper (free,
  local) turns your voice into text, and Fish Audio reads answers aloud.
- The website finds it automatically. Without it, the site keeps using the
  browser's own free voice.
- The Fish Audio key lives only in an environment variable on your computer.

## 2026-10-07: Every stock, every 15 seconds

- The Trader now refreshes all 101 S&P 100 stocks at once every 15 seconds,
  using one batched Yahoo request per 50 stocks.
- Signals, stops and the trailing stop are checked on the live price inside
  the minute, not just when the minute closes.
- Buy and short limits now use Yahoo's real bid.

## 2026-10-07: Facebook through Buffer, Etsy sheets (free tier)

- The social bot can now post to a **Facebook Page through Buffer's free
  plan**. It only sends posts you approved, with every placeholder filled in,
  at most 2 a day, and it does a dry run unless you add `--live` and switch
  `publishing_mode` to `buffer`.
- Approving a social post can fill in its text and link in the same step.
- The studio exports approved listings as a sheet to paste into Printify
  (t-shirts) or Etsy (printables). Nothing uploads to Etsy automatically,
  since every listing costs $0.20.
- 19 commerce tests.

## 2026-10-07: Back to the S&P 100

- The Trader scans the **S&P 100** again (101 stocks), matching the website's
  scanner. With fewer stocks, every one is refreshed about every 2 minutes.

## 2026-10-07: The Trader console

- `python terrarium.py` starts everything: it loads the day's price history,
  then every minute shows the top 10 stocks and which requirements each one
  meets (RSI, trend, fresh data, side allowed, no open trade).
- Entries are now **limit orders at the bid**, cancelled if they don't fill
  in 10 seconds. Still one trade at a time. Stops exit at market.
- Every signal is published to a **signal ledger** for the website, each
  scored as a $1,000 trade, even ones the bot can't take.
- Still a dry run: orders are simulated.

## 2026-10-07: Phase 2 commerce bots, draft-only

- Four new agents: **Social** (trends in, promo posts out), **Etsy research**
  (scores niches from keyword exports), **Studio** (original t-shirt and
  printable listing drafts), and **Content** (blog drafts with Amazon
  affiliate and AdSense slots).
- They hand work to each other through a shared board, and everything they
  make waits in an **approval queue**. Nothing is listed, posted or published.
- Built around the rules: no scraping Etsy, original designs only, AI and
  production-partner disclosures on every listing, trademark names flagged,
  affiliate disclosure first in every post.
- Accounts, APIs and costs still to decide: `docs/phase2-commerce-bots.md`.
- 13 new tests.

## 2026-10-07: Live market data (Yahoo Finance)

- The Trader now reads real prices from Yahoo Finance: a year of daily bars
  (cached once a day) and 1-minute bars all session.
- It stays inside a request budget: the 10 names it's watching refresh every
  minute, and the rest of the S&P 500 rotates through.
- If Yahoo slows down or blocks, it backs off and won't open a trade on stale
  prices.
- `--task trade` now runs the full minute loop on live data in **dry run**.
  Trades are simulated, and no orders go anywhere.
- 38 tests.

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
