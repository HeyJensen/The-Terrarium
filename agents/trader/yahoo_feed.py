"""Yahoo Finance market data (unofficial, free, no API key).

Uses Yahoo's public endpoints with the standard library only:
  /v7/finance/quote?symbols=A,B,...                 price, bid/ask, day volume for ~50 symbols per request
  /v8/finance/chart/<SYMBOL>?interval=1m&range=1d   today's 1-minute bars
  /v8/finance/chart/<SYMBOL>?interval=1d&range=1y   a year of daily bars

Fast mode (quotes available): every step (15 s) one batched quote snapshot
covers the whole S&P 100 in 3 requests and updates each stock's forming
1-minute bar. Chart requests only seed today's bars once per stock and
re-sync the stocks being watched once a minute. About 25 requests a minute.

Fallback mode (quote endpoint refused): the per-stock rotation below.

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
QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
QUOTE_FIELDS = "symbol,regularMarketPrice,regularMarketVolume,regularMarketTime,bid,ask"
QUOTE_CHUNK = 50
USER_AGENT = "Mozilla/5.0 (terrarium-trader; personal use)"


def yahoo_symbol(symbol: str) -> str:
    return symbol.replace(".", "-")  # BRK.B -> BRK-B


def urllib_get_json(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


class YahooSession:
    """Cookie + crumb session the batched quote endpoint needs (the same handshake
    yfinance does): visit fc.yahoo.com for a cookie, then ask for a crumb."""

    def __init__(self, timeout: float = 10.0):
        import http.cookiejar
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.timeout = timeout
        self.crumb: str | None = None

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        with self.opener.open(req, timeout=self.timeout) as resp:
            return resp.read()

    def _ensure_crumb(self) -> str:
        if not self.crumb:
            try:
                self._get("https://fc.yahoo.com")
            except urllib.error.HTTPError:
                pass  # usually a 404 that still sets the cookie
            self.crumb = self._get("https://query2.finance.yahoo.com/v1/test/getcrumb").decode().strip()
        return self.crumb

    def quotes(self, symbols: list[str]) -> list[dict]:
        for attempt in (1, 2):
            crumb = self._ensure_crumb()
            url = (f"{QUOTE_URL}?symbols={urllib.parse.quote(','.join(yahoo_symbol(s) for s in symbols))}"
                   f"&fields={QUOTE_FIELDS}&crumb={urllib.parse.quote(crumb)}")
            try:
                payload = json.loads(self._get(url))
                return (payload.get("quoteResponse") or {}).get("result") or []
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and attempt == 1:
                    self.crumb = None  # stale crumb: redo the handshake once
                    continue
                raise
        return []

    def get_json(self, url: str) -> dict:
        return json.loads(self._get(url))


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
                 get_json=urllib_get_json, sleep=time.sleep, request_spacing_s: float = 0.2,
                 quotes_fn=None, steps_per_minute: int = 1, seed_per_step: int = 8):
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
        self.quotes_fn = quotes_fn            # symbols -> [quote dicts]; None = fallback rotation only
        self.steps_per_minute = max(1, steps_per_minute)
        self.seed_per_step = seed_per_step
        self._chart_synced: dict[str, datetime] = {}
        self._forming: dict[str, Bar] = {}    # current minute, built from snapshots
        self._snap: dict[str, dict] = {}      # latest snapshot per symbol: price, bid, ask, volume, time
        self._prev_cumvol: dict[str, float] = {}
        self._quote_backoff_until: datetime | None = None

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
        """Spend this step's request budget. Fast mode: one batched snapshot of every
        stock, then chart seeding/re-sync. Fallback: hot symbols first, then round-robin."""
        today = to_et(now).date()
        budget = max(1, self.max_rpm // self.steps_per_minute)
        hot = [s for s in dict.fromkeys(hot) if s]
        if self.quotes_fn and self._snapshot(now, today):
            budget -= -(-len(self.universe) // QUOTE_CHUNK)
            for sym in [s for s in self.universe if s not in self._chart_synced][:self.seed_per_step] + hot:
                last = self._chart_synced.get(sym)
                if budget <= 0:
                    break
                if last is None or (now - last) >= timedelta(seconds=60):
                    budget -= self._refresh_minute(sym, today, now)
        else:
            for sym in hot:
                if budget <= 0:
                    break
                budget -= self._refresh_minute(sym, today, now)
        # Fill daily-history gaps (if prefetch wasn't run).
        for sym in self.universe:
            if budget <= 0:
                break
            if self._load_daily_cache(sym, today) is None:
                budget -= 1
                bars = self._fetch(sym, "1d", "1y", now)
                if bars:
                    self._store_daily(sym, today, bars)
        if not (self.quotes_fn and self._quote_ok):
            for _ in range(len(self._queue)):
                if budget <= 0:
                    break
                sym = self._queue[0]
                self._queue.rotate(-1)
                if sym not in hot:
                    budget -= self._refresh_minute(sym, today, now)
        self._log_lag(hot, now)

    _quote_ok = False

    def _snapshot(self, now: datetime, today: date) -> bool:
        """One batched quote call for the whole universe; updates each forming bar."""
        if self._quote_backoff_until and now < self._quote_backoff_until:
            self._quote_ok = False
            return False
        rows = []
        try:
            for i in range(0, len(self.universe), QUOTE_CHUNK):
                self.requests_made += 1
                rows += self.quotes_fn(self.universe[i:i + QUOTE_CHUNK])
        except Exception as e:  # any failure: fall back to chart rotation for a while
            self._quote_backoff_until = now + timedelta(minutes=5)
            self.logger.warning(f"yahoo quote snapshot failed ({type(e).__name__}); using chart rotation for 5 min")
            self._quote_ok = False
            return False
        by_yahoo = {yahoo_symbol(s): s for s in self.universe}
        for q in rows:
            sym = by_yahoo.get(q.get("symbol"))
            price, t = q.get("regularMarketPrice"), q.get("regularMarketTime")
            if sym is None or price is None or t is None:
                continue
            self._apply_quote(sym, float(price), float(q.get("regularMarketVolume") or 0),
                              datetime.fromtimestamp(int(t), timezone.utc), q.get("bid"), q.get("ask"), today)
        self._quote_ok = True
        return True

    def _apply_quote(self, sym: str, price: float, cumvol: float, at: datetime, bid, ask, today: date) -> None:
        if to_et(at).date() != today:
            return
        prev_cumvol = self._snap[sym]["volume"] if sym in self._snap else cumvol
        self._snap[sym] = {"price": price, "volume": cumvol, "time": at, "bid": bid, "ask": ask}
        minute = at.replace(second=0, microsecond=0)
        f = self._forming.get(sym)
        if f and f.ts == minute:
            self._forming[sym] = Bar(minute, f.open, max(f.high, price), min(f.low, price), price,
                                     max(cumvol - self._prev_cumvol[sym], 0.0))
            return
        if f:  # the previous minute is over: keep it unless the chart already has that bar
            self._finalize(sym, f, today)
        # Volume traded since the last snapshot belongs to the new minute.
        self._prev_cumvol[sym] = prev_cumvol
        self._forming[sym] = Bar(minute, price, price, price, price, max(cumvol - prev_cumvol, 0.0))

    def _finalize(self, sym: str, bar: Bar, today: date) -> None:
        day, bars = self._minute.get(sym, (today, []))
        if day != today:
            bars = []
        if all(b.ts != bar.ts for b in bars):
            bars = sorted(bars + [bar], key=lambda b: b.ts)
        self._minute[sym] = (today, bars)

    def forming_bar(self, symbol: str, now: datetime) -> Bar | None:
        f = self._forming.get(symbol)
        if f and f.ts <= now < f.ts + timedelta(minutes=1):
            return f
        return None

    def today_volume(self, symbol: str, now: datetime) -> float | None:
        snap = self._snap.get(symbol)
        return snap["volume"] if snap and to_et(snap["time"]).date() == to_et(now).date() else None

    def bid_ask(self, symbol: str) -> tuple[float, float] | None:
        snap = self._snap.get(symbol)
        if snap and snap["bid"] and snap["ask"]:
            return float(snap["bid"]), float(snap["ask"])
        return None

    def _refresh_minute(self, symbol: str, today: date, now: datetime) -> int:
        bars = self._fetch(symbol, "1m", "1d", now)
        if bars is not None:
            self._chart_synced[symbol] = now
            # The chart's newest bar is usually the still-forming minute; keep only completed ones
            # here (the snapshot owns the forming bar), plus any snapshot-built bars it lacks.
            fresh = {b.ts: b for b in bars if to_et(b.ts).date() == today and b.ts + timedelta(minutes=1) <= now}
            old = self._minute.get(symbol, (today, []))
            for b in (old[1] if old[0] == today else []):
                fresh.setdefault(b.ts, b)
            self._minute[symbol] = (today, sorted(fresh.values(), key=lambda b: b.ts))
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
        f = self._forming.get(symbol)
        if f and f.ts + timedelta(minutes=1) <= now:  # minute over with no newer snapshot yet
            self._finalize(symbol, f, to_et(now).date())
            del self._forming[symbol]
            cached = self._minute.get(symbol)
        if not cached or cached[0] != to_et(now).date():
            return []
        return [b for b in cached[1] if b.ts + timedelta(minutes=1) <= now]
