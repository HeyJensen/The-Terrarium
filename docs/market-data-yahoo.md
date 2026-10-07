# Market data: Yahoo Finance

Nathan's call (2026-10-07): use Yahoo Finance for now.

## How it's wired

`agents/trader/yahoo_feed.py` calls Yahoo's public chart endpoint with the
Python standard library. **No new dependency** (not even `yfinance`).

| Need | Request | How often |
|---|---|---|
| A year of daily bars (20-day volume, 50/200 EMA) | `chart/SYM?interval=1d&range=1y` | once a day per symbol, cached on disk. Run `--task prefetch` before 9:30 ET. |
| Today's 1-minute bars | `chart/SYM?interval=1m&range=1d` | every minute for the current top 10 + any open position; the other ~90 in rotation |

Budget: `yahoo_max_requests_per_minute` (default 50, about 3,000 an hour).
At that rate the whole S&P 100 relative-volume ranking refreshes about
every 2 minutes, and the top 10 names (the ones RSI is checked on) refresh
every minute. In the first ~2 minutes after the open, not every symbol has
data yet, so the ranking only covers what's been fetched so far.

## What Yahoo's limits mean for the strategy

- **No official API, no published limits.** Yahoo throttles heavy use with
  HTTP 429 and blocks some non-browser clients. On 429/403/5xx the feed backs
  off (1 minute, doubling up to 15) and keeps the last good data.
- **Stale data can't open trades.** If the newest closed bar for a signal is
  more than `max_bar_age_minutes` (default 3) old, the entry is skipped and
  logged. Stops are still checked on whatever data is there, so a long Yahoo
  outage means stops are checked late.
- **Freshness is unmeasured.** Yahoo's 1-minute bars are generally near
  real-time for US stocks, but the newest bar can arrive late and its volume
  can be partial. The feed logs how old the newest bar is every minute, so
  the first dry-run day will tell us the real delay.
- **Volume isn't official consolidated volume.** Relative volume compares
  Yahoo's intraday volume to Yahoo's daily volume, so it's consistent with
  itself, but may differ from a broker's numbers.
- **It can break without notice.** Yahoo has changed or blocked these
  endpoints before. If plain requests get blocked, the usual fix is the
  `yfinance` library, which imitates a browser. It's free but pulls in
  pandas, numpy and curl_cffi, so it needs Nathan's OK first.
- **Terms: personal use only.** Yahoo's data is for personal use. Using it to
  run your own account fits that. Showing Yahoo-derived prices on the public
  website, or in paid Telegram alerts and Skool content, probably does not.
  Before Phase 3, move to a licensed data feed. (This is my reading, not
  legal advice.)

Sources: [yfinance on PyPI (disclaimer: "intended for personal use only")](https://pypi.org/project/yfinance/),
[yfinance issue #2422 (rate limiting, 2025)](https://github.com/ranaroussi/yfinance/issues/2422),
[Scrapfly guide to the Yahoo Finance endpoints](https://scrapfly.io/blog/posts/guide-to-yahoo-finance-api),
[Yahoo Terms of Service](https://legal.yahoo.com/us/en/frontier/terms/otos/index.html).

## Not tested against the real Yahoo yet

This cloud sandbox's network policy blocks Yahoo's hosts, so the feed is
tested against Yahoo-shaped sample responses only. The first real check is
running it on Nathan's computer:

```bash
python agents/trader/run.py --task prefetch   # before the open
python agents/trader/run.py --task trade      # dry run on live data, Ctrl+C to stop
```
