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
