"""Yahoo Finance market data (unofficial, free, no API key).

Uses Yahoo's public chart endpoint with the standard library only:
  /v8/finance/chart/<SYMBOL>?interval=1m&range=1d   today's 1-minute bars
  /v8/finance/chart/<SYMBOL>?interval=1d&range=1y   a year of daily bars

Yahoo has no official API or published limits and throttles heavy use, so:
- Daily history is fetched once per day per symbol and cached on disk
  (run `--task prefetch` before the open; about 2 minutes for the 101 S&P 100 symbols).
- Every minute we stay under `max_requests_per_minute`: first the "hot" symbols
  (current top-N by relative volume + any open position), then the rest of the
  universe round-robin. With the 101 S&P 100 symbols and 50 requests/minute the full
  relative-volume ranking refreshes about every 2 minutes; the top names are
  refreshed every minute.
- On 429/403/5xx we back off (1 min doubling to 15 min) and keep serving the
  last good data. The engine refuses new entries on stale bars.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from shared.market_calendar import to_et

from agents.trader.data_feed import Bar, MarketData

BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
USER_AGENT = "Mozilla/5.0 (terrarium-trader; personal use)"


def yahoo_symbol(symbol: str) -> str:
    return symbol.replace(".", "-")  # BRK.B -> BRK-B


def urllib_get_json(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def parse_chart(payload: dict) -> list[Bar]:
    """Turn a v8 chart response into Bars, skipping rows with missing values."""
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise ValueError(f"yahoo error: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        return []
    r = results[0]
    stamps = r.get("timestamp") or []
    q = (r.get("indicators", {}).get("quote") or [{}])[0]
    bars = []
    for i, ts in enumerate(stamps):
        try:
            o, h, l, c, v = (q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i])
        except (KeyError, IndexError):
            continue
        if None in (o, h, l, c):
            continue
        bars.append(Bar(datetime.fromtimestamp(ts, timezone.utc), float(o), float(h), float(l), float(c), float(v or 0)))
    return bars


class YahooFeed(MarketData):
    def __init__(self, universe: list[str], cache_dir: Path, logger, max_requests_per_minute: int = 50,
                 get_json=urllib_get_json, sleep=time.sleep, request_spacing_s: float = 0.2):
        self.universe = list(universe)
        self.cache_dir = Path(cache_dir)
        self.logger = logger
        self.max_rpm = max_requests_per_minute
        self.get_json = get_json
        self.sleep = sleep
        self.spacing = request_spacing_s
        self._minute: dict[str, tuple[date, list[Bar]]] = {}
        self._daily: dict[str, tuple[date, list[Bar]]] = {}
        self._queue = deque(self.universe)
        self._backoff_until: datetime | None = None
        self._backoff_s = 60
        self.requests_made = 0

    # ---- HTTP with backoff ---------------------------------------------
    def _fetch(self, symbol: str, interval: str, rng: str, now: datetime) -> list[Bar] | None:
        if self._backoff_until and now < self._backoff_until:
            return None
        url = f"{BASE_URL}{urllib.parse.quote(yahoo_symbol(symbol))}?interval={interval}&range={rng}"
        self.requests_made += 1
        try:
            bars = parse_chart(self.get_json(url))
            self._backoff_s = 60
            return bars
        except urllib.error.HTTPError as e:
            if e.code in (429, 403) or e.code >= 500:
                self._backoff_until = now + timedelta(seconds=self._backoff_s)
                self.logger.warning(f"yahoo HTTP {e.code}; backing off {self._backoff_s}s, serving cached data")
                self._backoff_s = min(self._backoff_s * 2, 900)
            else:
                self.logger.warning(f"yahoo HTTP {e.code} for {symbol}")
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            self.logger.warning(f"yahoo fetch failed for {symbol}: {type(e).__name__}: {e}")
        return None

    # ---- daily history (cached on disk per day) -------------------------
    def _daily_path(self, symbol: str, day: date) -> Path:
        return self.cache_dir / "daily" / day.isoformat() / f"{symbol}.json"

    def _load_daily_cache(self, symbol: str, day: date) -> list[Bar] | None:
        if symbol in self._daily and self._daily[symbol][0] == day:
            return self._daily[symbol][1]
        p = self._daily_path(symbol, day)
        if not p.exists():
            return None
        bars = [Bar(datetime.fromisoformat(b[0]), *b[1:]) for b in json.loads(p.read_text())]
        self._daily[symbol] = (day, bars)
        return bars

    def _store_daily(self, symbol: str, day: date, bars: list[Bar]) -> None:
        p = self._daily_path(symbol, day)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps([[b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume] for b in bars]))
        self._daily[symbol] = (day, bars)

    def prefetch_daily(self, now: datetime) -> tuple[int, int]:
        """Fetch a year of daily bars for every symbol not cached today. Returns (ok, failed)."""
        day = to_et(now).date()
        ok = failed = 0
        for sym in self.universe:
            if self._load_daily_cache(sym, day) is not None:
                ok += 1
                continue
            bars = self._fetch(sym, "1d", "1y", datetime.now(timezone.utc))
            if bars:
                self._store_daily(sym, day, bars)
                ok += 1
            else:
                failed += 1
            self.sleep(60.0 / self.max_rpm)
        return ok, failed

    def _daily_before(self, symbol: str, day: date) -> list[Bar]:
        bars = self._load_daily_cache(symbol, day) or []
        return [b for b in bars if to_et(b.ts).date() < day]

    def prior_daily_volumes(self, symbol: str, day: date, n: int) -> list[float]:
        return [b.volume for b in self._daily_before(symbol, day)][-n:]

    def prior_daily_closes(self, symbol: str, day: date, n: int) -> list[float]:
        return [b.close for b in self._daily_before(symbol, day)][-n:]

    # ---- intraday ------------------------------------------------------
    def prepare(self, now: datetime, hot: list[str]) -> None:
        """Spend this minute's request budget: hot symbols first, then round-robin."""
        today = to_et(now).date()
        budget = self.max_rpm
        hot = [s for s in dict.fromkeys(hot) if s]
        for sym in hot:
            if budget <= 0:
                break
            budget -= self._refresh_minute(sym, today, now)
        # Fill daily-history gaps (if prefetch wasn't run) before cycling the rest.
        for sym in self.universe:
            if budget <= 0:
                break
            if self._load_daily_cache(sym, today) is None:
                budget -= 1
                bars = self._fetch(sym, "1d", "1y", now)
                if bars:
                    self._store_daily(sym, today, bars)
        for _ in range(len(self._queue)):
            if budget <= 0:
                break
            sym = self._queue[0]
            self._queue.rotate(-1)
            if sym not in hot:
                budget -= self._refresh_minute(sym, today, now)
        self._log_lag(hot, now)

    def _refresh_minute(self, symbol: str, today: date, now: datetime) -> int:
        bars = self._fetch(symbol, "1m", "1d", now)
        if bars is not None:
            self._minute[symbol] = (today, [b for b in bars if to_et(b.ts).date() == today])
        if self.spacing:
            self.sleep(self.spacing)
        return 1

    def _log_lag(self, hot: list[str], now: datetime) -> None:
        lags = []
        for sym in hot:
            bars = self.minute_bars(sym, now)
            if bars:
                lags.append((now - (bars[-1].ts + timedelta(minutes=1))).total_seconds())
        if lags:
            self.logger.info(f"yahoo freshness: newest closed bar is {min(lags):.0f}-{max(lags):.0f}s old "
                             f"across {len(lags)} hot symbols; {self.requests_made} requests so far")

    def minute_bars(self, symbol: str, now: datetime) -> list[Bar]:
        cached = self._minute.get(symbol)
        if not cached or cached[0] != to_et(now).date():
            return []
        return [b for b in cached[1] if b.ts + timedelta(minutes=1) <= now]
