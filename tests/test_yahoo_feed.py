import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from shared.logger import get_logger
from shared.market_calendar import ET

from agents.trader.broker import SimBroker
from agents.trader.yahoo_feed import YahooFeed, parse_chart

NOW = datetime(2026, 10, 7, 10, 30, 5, tzinfo=ET)  # Wednesday mid-morning


def chart(stamps, closes, volume=1000, nulls=()):
    q = {"open": list(closes), "high": [c + 0.1 for c in closes], "low": [c - 0.1 for c in closes],
         "close": list(closes), "volume": [volume] * len(closes)}
    for i in nulls:
        for k in q:
            q[k][i] = None
    return {"chart": {"result": [{"timestamp": stamps, "indicators": {"quote": [q]}}], "error": None}}


class FakeYahoo:
    """Answers chart URLs with synthetic data and records every URL requested."""
    def __init__(self):
        self.urls = []
        self.fail_with = None

    def __call__(self, url):
        self.urls.append(url)
        if self.fail_with:
            raise urllib.error.HTTPError(url, self.fail_with, "x", {}, None)
        if "interval=1m" in url:
            start = datetime(2026, 10, 7, 9, 30, tzinfo=ET)
            stamps = [int((start + timedelta(minutes=i)).timestamp()) for i in range(61)]
            return chart(stamps, [100 + i * 0.01 for i in range(61)])
        start = datetime(2025, 10, 7, 9, 30, tzinfo=ET)
        days = [start + timedelta(days=i) for i in range(366)]
        days = [d for d in days if d.weekday() < 5]
        return chart([int(d.timestamp()) for d in days], [50 + i * 0.1 for i in range(len(days))], volume=2_000_000)


class YahooFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = get_logger("yahoo-test", Path(self.tmp.name) / "logs")
        self.fake = FakeYahoo()

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, universe, rpm=20):
        return YahooFeed(universe, Path(self.tmp.name) / "cache", self.logger, max_requests_per_minute=rpm,
                         get_json=self.fake, sleep=lambda s: None, request_spacing_s=0)

    def test_parse_skips_missing_rows_and_raises_on_error(self):
        bars = parse_chart(chart([1, 2, 3], [1.0, 2.0, 3.0], nulls=(1,)))
        self.assertEqual([b.close for b in bars], [1.0, 3.0])
        with self.assertRaises(ValueError):
            parse_chart({"chart": {"result": None, "error": {"code": "Not Found"}}})

    def test_minute_budget_hot_first_then_round_robin(self):
        universe = [f"S{i:03d}" for i in range(100)]
        f = self.feed(universe, rpm=20)
        f.prefetch_daily(NOW)
        self.fake.urls.clear()
        f.prepare(NOW, hot=["S050", "S051"])
        self.assertEqual(len(self.fake.urls), 20)
        self.assertIn("S050", self.fake.urls[0]); self.assertIn("S051", self.fake.urls[1])
        # Bars are returned only once closed, and only for today's session.
        bars = f.minute_bars("S050", NOW)
        self.assertEqual(bars[-1].ts + timedelta(minutes=1), datetime(2026, 10, 7, 10, 30, tzinfo=ET))
        # Hot symbols refresh every minute; the rest rotate through the whole universe.
        seen = set()
        for i in range(6):
            self.fake.urls.clear()
            f.prepare(NOW + timedelta(minutes=i + 1), hot=["S050"])
            self.assertIn("S050", self.fake.urls[0])
            seen.update(u.split("/chart/")[1].split("?")[0] for u in self.fake.urls)
        self.assertEqual(seen, set(universe))

    def test_backoff_on_429_serves_cached_data(self):
        f = self.feed(["AAPL"], rpm=5)
        f.prefetch_daily(NOW)
        f.prepare(NOW, hot=["AAPL"])
        cached = f.minute_bars("AAPL", NOW)
        self.assertTrue(cached)
        self.fake.fail_with = 429
        f.prepare(NOW + timedelta(minutes=1), hot=["AAPL"])
        n = len(self.fake.urls)
        f.prepare(NOW + timedelta(minutes=1, seconds=30), hot=["AAPL"])  # inside the 60s backoff
        self.assertEqual(len(self.fake.urls), n)
        self.assertEqual(f.minute_bars("AAPL", NOW), cached)

    def test_daily_history_cached_on_disk_and_excludes_today(self):
        f = self.feed(["AAPL", "BRK.B"])
        ok, failed = f.prefetch_daily(NOW)
        self.assertEqual((ok, failed), (2, 0))
        self.assertTrue(any("BRK-B" in u for u in self.fake.urls))
        n = len(self.fake.urls)
        f2 = self.feed(["AAPL", "BRK.B"])
        closes = f2.prior_daily_closes("AAPL", NOW.date(), 252)
        self.assertEqual(len(self.fake.urls), n)  # served from disk
        self.assertGreater(len(closes), 200)
        self.assertEqual(len(f2.prior_daily_volumes("AAPL", NOW.date(), 20)), 20)
        last_day = max(b.ts for b in f2._daily_before("AAPL", NOW.date()))
        self.assertLess(last_day.astimezone(ET).date(), NOW.date())


class FastSnapshots(unittest.TestCase):
    """15-second mode: batched quotes for every stock, building the forming 1-minute bar."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = get_logger("yahoo-fast-test", Path(self.tmp.name) / "logs")
        self.fake = FakeYahoo()
        self.quote_calls = []
        self.prices = {}

    def tearDown(self):
        self.tmp.cleanup()

    def quotes(self, symbols):
        self.quote_calls.append(list(symbols))
        return [{"symbol": s.replace(".", "-"), "regularMarketPrice": self.prices[s][0],
                 "regularMarketVolume": self.prices[s][1], "regularMarketTime": int(self.prices[s][2].timestamp()),
                 "bid": self.prices[s][0] - 0.01, "ask": self.prices[s][0] + 0.01} for s in symbols]

    def test_whole_universe_in_three_requests_per_step(self):
        universe = [f"S{i:03d}" for i in range(101)]
        f = YahooFeed(universe, Path(self.tmp.name) / "cache", self.logger, max_requests_per_minute=50,
                      get_json=self.fake, sleep=lambda s: None, request_spacing_s=0, quotes_fn=self.quotes,
                      steps_per_minute=4, seed_per_step=8)
        f.prefetch_daily(NOW)
        self.fake.urls.clear()
        t = NOW.replace(second=15)
        self.prices = {s: (100.0, 1_000_000, t) for s in universe}
        f.prepare(t, hot=[])
        self.assertEqual([len(c) for c in self.quote_calls], [50, 50, 1])
        self.assertLessEqual(len(self.fake.urls), 12 - 3)  # chart seeding stays inside the step budget
        self.assertEqual(f.bid_ask("S000"), (99.99, 100.01))
        self.assertEqual(f.today_volume("S000", t), 1_000_000)

    def test_forming_bar_updates_then_closes_with_volume(self):
        f = YahooFeed(["AAPL"], Path(self.tmp.name) / "cache", self.logger, get_json=self.fake,
                      sleep=lambda s: None, request_spacing_s=0, quotes_fn=self.quotes, steps_per_minute=4,
                      seed_per_step=0)
        f._chart_synced["AAPL"] = NOW  # skip chart seeding for this test
        m = NOW.replace(second=0, microsecond=0)
        for sec, price, vol in [(5, 100.0, 1000), (20, 101.0, 1300), (35, 99.5, 1600), (50, 100.5, 2000)]:
            self.prices = {"AAPL": (price, vol, m + timedelta(seconds=sec))}
            f.prepare(m + timedelta(seconds=sec + 1), hot=[])
        bar = f.forming_bar("AAPL", m + timedelta(seconds=55))
        self.assertEqual((bar.open, bar.high, bar.low, bar.close), (100.0, 101.0, 99.5, 100.5))
        self.assertEqual(bar.volume, 1000)  # traded since the first snapshot of the minute
        self.prices = {"AAPL": (100.7, 2300, m + timedelta(minutes=1, seconds=5))}
        f.prepare(m + timedelta(minutes=1, seconds=6), hot=[])
        closed = f.minute_bars("AAPL", m + timedelta(minutes=1, seconds=6))
        self.assertEqual(closed[-1].ts, m)
        self.assertEqual(closed[-1].close, 100.5)
        self.assertEqual(f.forming_bar("AAPL", m + timedelta(minutes=1, seconds=6)).volume, 300)

    def test_quote_failure_falls_back_to_chart_rotation(self):
        def broken(symbols):
            raise urllib.error.HTTPError("u", 401, "x", {}, None)
        f = YahooFeed(["AAPL", "MSFT"], Path(self.tmp.name) / "cache", self.logger, max_requests_per_minute=10,
                      get_json=self.fake, sleep=lambda s: None, request_spacing_s=0, quotes_fn=broken)
        f.prefetch_daily(NOW)
        f.prepare(NOW, hot=["AAPL"])
        self.assertTrue(f.minute_bars("AAPL", NOW))
        self.assertTrue(f.minute_bars("MSFT", NOW))


class SimBrokerPersistence(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sim.json"
            b = SimBroker(1000, "cash", 0)
            now = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
            b.place_market_order("X", "buy", 5, 100, now)
            b.place_market_order("X", "sell", 5, 101, now)
            b.save(p)
            b2 = SimBroker.load_or_new(p, 1000, "cash", 0)
            self.assertAlmostEqual(b2.account({}).cash, 1005)
            self.assertAlmostEqual(b2.account({}).settled_cash, 500)
