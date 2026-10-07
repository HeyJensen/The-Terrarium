import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from shared import risk
from shared.dashboard_client import build_status
from shared.logger import DecisionLog, get_logger
from shared.market_calendar import ET, is_regular_hours, trading_days_between
from shared.notifications import Notifier, format_trade_alert

from agents.trader import strategy
from agents.trader.broker import SimBroker
from agents.trader.data_feed import ReplayFeed
from agents.trader.engine import TraderEngine
from agents.trader.indicators import ema, relative_volume, rsi
from tests import fixtures

MON, TUE, WED = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)

BASE_CFG = {
    "order_routing": "dry_run", "phase": "shakedown", "mode": "cash", "account_size_usd": 1000,
    "risk_per_trade_pct": 1.0, "stop_loss_pct": 1.0, "profit_trigger_pct": 2.0, "after_profit_trigger": "trail",
    "trail_pct": 1.0, "max_concurrent_positions": 1,
    "max_hold_days": 5, "daily_loss_limit_pct": 3.0, "kill_switch_flattens_position": True,
    "allow_fractional_shares": False, "top_n_by_relative_volume": 10, "relative_volume_lookback_days": 20,
    "rsi_period": 14, "rsi_short_at_or_above": 85, "rsi_long_at_or_below": 15,
    "trend_filter_enabled": True, "trend_fast_ema": 50, "trend_slow_ema": 200, "trend_lookback_days": 252,
}


class Indicators(unittest.TestCase):
    def test_rsi_extremes_and_warmup(self):
        self.assertIsNone(rsi([1.0] * 14))
        self.assertEqual(rsi([float(i) for i in range(30)]), 100.0)
        self.assertEqual(rsi([float(30 - i) for i in range(30)]), 0.0)

    def test_rsi_matches_wilder_reference(self):
        # Classic Wilder example series; reference RSI after 15 closes ~= 70.53
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
                  45.89, 46.03, 45.61, 46.28, 46.28]
        self.assertAlmostEqual(rsi(closes), 70.53, delta=0.1)

    def test_ema(self):
        self.assertIsNone(ema([1.0] * 49, 50))
        self.assertAlmostEqual(ema([5.0] * 300, 200), 5.0)
        rising = [float(i) for i in range(252)]
        self.assertGreater(ema(rising, 50), ema(rising, 200))

    def test_relative_volume(self):
        self.assertAlmostEqual(relative_volume(3_000_000, [1_000_000] * 20), 3.0)
        self.assertIsNone(relative_volume(10, []))


class Strategy(unittest.TestCase):
    def test_signals(self):
        self.assertEqual(strategy.entry_signal(85, 85, 15), "short")
        self.assertIsNone(strategy.entry_signal(84.9, 85, 15))
        self.assertEqual(strategy.entry_signal(15, 85, 15), "long")
        self.assertIsNone(strategy.entry_signal(50, 85, 15))

    def test_trend_filter(self):
        self.assertTrue(strategy.trend_allows("short", 90, 100))
        self.assertFalse(strategy.trend_allows("short", 110, 100))
        self.assertTrue(strategy.trend_allows("long", 110, 100))
        self.assertFalse(strategy.trend_allows("long", 90, 100))
        self.assertFalse(strategy.trend_allows("long", None, 100))

    def test_stop_ratchet(self):
        # below the +2% trigger: stop stays at -1%
        self.assertEqual(strategy.ratchet_stop("long", 100, 101.9, 99, 102, "trail", 1), 99)
        # at +2%: trail 1% behind the high (~ +1% locked), breakeven moves to entry
        self.assertAlmostEqual(strategy.ratchet_stop("long", 100, 102, 99, 102, "trail", 1), 100.98)
        self.assertEqual(strategy.ratchet_stop("long", 100, 102, 99, 102, "breakeven", 1), 100)
        # never moves backwards
        self.assertEqual(strategy.ratchet_stop("long", 100, 102, 101.5, 102, "breakeven", 1), 101.5)
        # shorts mirror it
        self.assertAlmostEqual(strategy.ratchet_stop("short", 100, 98, 101, 98, "trail", 1), 98.98)

    def test_levels(self):
        t, s = strategy.initial_levels(100, "long", 2, 1)
        self.assertAlmostEqual(t, 102); self.assertAlmostEqual(s, 99)
        t, s = strategy.initial_levels(100, "short", 2, 1)
        self.assertAlmostEqual(t, 98); self.assertAlmostEqual(s, 101)

    def test_same_bar_both_levels_assumes_stop(self):
        self.assertEqual(strategy.bar_exit("long", 100, 103, 98, stop=99, target=102)[0], "stop_loss")
        self.assertIsNone(strategy.bar_exit("long", 100, 103, 99.5, stop=99))  # no fixed target in trail mode

    def test_gap_fills_at_open(self):
        self.assertEqual(strategy.bar_exit("long", 95, 96, 94, stop=99, target=102), ("stop_loss_gap", 95))

    def test_sizing_is_one_full_position(self):
        self.assertEqual(strategy.position_size(1000, 1, 1, 100, 1000, False), 10)
        self.assertEqual(strategy.position_size(1000, 1, 1, 333, 1000, False), 3)
        self.assertEqual(strategy.position_size(1000, 1, 1, 100, 450, False), 4)  # capped by cash


class Guardrails(unittest.TestCase):
    def test_config_cannot_loosen_limits(self):
        lim = risk.effective_limits({**BASE_CFG, "daily_loss_limit_pct": 10, "max_concurrent_positions": 3,
                                     "risk_per_trade_pct": 5})
        self.assertEqual(lim["daily_loss_limit_pct"], 3.0)
        self.assertEqual(lim["max_concurrent_positions"], 1)
        self.assertEqual(lim["risk_per_trade_pct"], 1.0)

    def test_scaling_needs_approval(self):
        with self.assertRaises(risk.GuardrailViolation):
            risk.effective_limits({**BASE_CFG, "account_size_usd": 2000})
        with self.assertRaises(risk.GuardrailViolation):
            risk.effective_limits({**BASE_CFG, "mode": "yolo"})

    def test_kill_switch_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "k.json"
            k = risk.KillSwitch(3.0, p)
            k.start_day(MON, 1000)
            self.assertFalse(k.check(980))
            self.assertTrue(k.check(969))
            k2 = risk.KillSwitch(3.0, p)
            k2.start_day(MON, 900)  # same day: must not reset
            self.assertTrue(k2.tripped)
            k2.start_day(TUE, 969)
            self.assertFalse(k2.tripped)

    def test_market_hours(self):
        at = lambda d, h, m: datetime(d.year, d.month, d.day, h, m, tzinfo=ET)
        self.assertFalse(is_regular_hours(at(MON, 9, 29)))
        self.assertTrue(is_regular_hours(at(MON, 9, 30)))
        self.assertTrue(is_regular_hours(at(MON, 15, 59)))
        self.assertFalse(is_regular_hours(at(MON, 16, 0)))
        self.assertFalse(is_regular_hours(at(date(2026, 10, 10), 11, 0)))  # Saturday
        self.assertFalse(is_regular_hours(at(date(2026, 11, 26), 11, 0)))  # Thanksgiving
        self.assertFalse(is_regular_hours(at(date(2026, 11, 27), 13, 30)))  # early close
        self.assertEqual(trading_days_between(date(2026, 10, 9), date(2026, 10, 12)), 1)

    def test_sim_broker_cash_settlement(self):
        b = SimBroker(1000, "cash", slippage_bps=0)
        now = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
        b.place_market_order("X", "buy", 10, 100, now)
        b.place_market_order("X", "sell", 10, 101, now)
        acct = b.account({})
        self.assertAlmostEqual(acct.cash, 1010)
        self.assertAlmostEqual(acct.settled_cash, 0)
        with self.assertRaises(ValueError):
            b.place_market_order("X", "buy", 1, 100, now)  # would be a good-faith violation
        with self.assertRaises(ValueError):
            b.place_market_order("X", "sell_short", 1, 100, now)  # cash account
        b.place_market_order("X", "buy", 1, 100, now + timedelta(days=1))  # settled next day
        self.assertAlmostEqual(b.account({}).settled_cash, 910)


class EngineReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.logger = get_logger("test-trader", log_dir=self.root / "logs")

    def tearDown(self):
        self.tmp.cleanup()

    def _quiet(self, symbol, days, price=50.0):
        fixtures.write_symbol(self.data, symbol, {d: fixtures.path_bars(fixtures.pad([price]), 1000) for d in days})

    def run_engine(self, cfg=None, broker=None):
        cfg = {**BASE_CFG, **(cfg or {})}
        feed = ReplayFeed(self.data)
        broker = broker or SimBroker(1000, cfg["mode"], slippage_bps=0)
        decisions = DecisionLog("trader", log_dir=self.root / "logs")
        engine = TraderEngine(cfg, broker, feed, feed.symbols(), self.root / "state", decisions, self.logger,
                              Notifier({}, self.logger))
        for now in feed.timeline():
            engine.step(now)
        return engine, broker, decisions.read()

    def test_long_entry_then_trailing_stop(self):
        up = [98.2 + 0.1 * i for i in range(40)]           # rallies past +2% to ~102.1
        closes = fixtures.selloff_then(100, 20, 0.1, up + [up[-1] - 0.1 * i for i in range(1, 30)])
        fixtures.write_symbol(self.data, "AAPL", {MON: fixtures.path_bars(fixtures.pad(closes), 50_000)})
        self._quiet("MSFT", [MON])
        engine, broker, log = self.run_engine()
        entries = [e for e in log if e["action"] == "entry"]
        exits = [e for e in log if e["action"] == "exit"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["side"], "long")
        self.assertEqual(entries[0]["symbol"], "AAPL")
        self.assertLessEqual(entries[0]["rsi"], 15)
        self.assertIn("rel_vol", entries[0])
        self.assertTrue([e for e in log if e["action"] == "stop_moved"])
        self.assertEqual(exits[0]["reasoning"], "stop_loss")  # the trailed stop, now in profit
        self.assertGreater(exits[0]["pnl"], 0)
        self.assertGreater(exits[0]["exit_price"], entries[0]["entry_price"] * 1.009)
        self.assertIsNone(engine.position)
        # Cash account: proceeds are unsettled, so later signals the same day can't trade.
        self.assertEqual(len(broker.fills), 2)

    def test_stop_loss(self):
        closes = fixtures.selloff_then(100, 20, 0.1, [98.0 - 0.1 * i for i in range(20)])
        fixtures.write_symbol(self.data, "AAPL", {MON: fixtures.path_bars(fixtures.pad(closes), 50_000)})
        engine, broker, log = self.run_engine()
        exits = [e for e in log if e["action"] == "exit"]
        self.assertEqual(exits[0]["reasoning"], "stop_loss")
        self.assertLess(exits[0]["pnl"], 0)

    def test_short_skipped_in_cash_mode(self):
        closes = [100 + 0.1 * i for i in range(25)]
        fixtures.write_symbol(self.data, "TSLA", {MON: fixtures.path_bars(fixtures.pad(closes), 50_000)}, trend="down")
        engine, broker, log = self.run_engine()
        self.assertEqual(broker.fills, [])
        skips = [e for e in log if e["action"] == "skip_signal"]
        self.assertTrue(skips and "cash mode is long-only" in skips[0]["reasoning"])

    def test_margin_mode_below_2000_stays_long_only(self):
        closes = [100 + 0.1 * i for i in range(25)]
        fixtures.write_symbol(self.data, "TSLA", {MON: fixtures.path_bars(fixtures.pad(closes), 50_000)}, trend="down")
        engine, broker, log = self.run_engine({"mode": "margin"}, SimBroker(1000, "margin", slippage_bps=0))
        self.assertEqual(broker.fills, [])
        self.assertIn("$2,000+", [e for e in log if e["action"] == "skip_signal"][0]["reasoning"])

    def test_margin_short_with_easy_to_borrow_check(self):
        closes = [100 + 0.1 * i for i in range(25)] + [102.4 - 0.1 * i for i in range(45)]
        fixtures.write_symbol(self.data, "TSLA", {MON: fixtures.path_bars(fixtures.pad(closes), 50_000)}, trend="down")
        engine, broker, log = self.run_engine({"mode": "margin", "after_profit_trigger": "take_profit"},
                                              SimBroker(2500, "margin", slippage_bps=0))
        entry = [e for e in log if e["action"] == "entry"][0]
        self.assertEqual(entry["side"], "short")
        self.assertEqual(entry["qty"], 9)  # sized off the $1,000 base, not the $2,500 equity
        self.assertEqual([e for e in log if e["action"] == "exit"][0]["reasoning"], "take_profit")

        # Same setup, but the name is hard to borrow -> skipped.
        self.tmp.cleanup(); self.setUp()
        fixtures.write_symbol(self.data, "TSLA", {MON: fixtures.path_bars(fixtures.pad(closes), 50_000)}, trend="down")
        engine, broker, log = self.run_engine({"mode": "margin"},
                                              SimBroker(2500, "margin", slippage_bps=0, hard_to_borrow={"TSLA"}))
        self.assertFalse([f for f in broker.fills if f.side == "sell_short"])
        self.assertTrue([e for e in log if "easy-to-borrow" in e["reasoning"]])

    def test_breakeven_mode(self):
        up = [98.2 + 0.1 * i for i in range(40)]
        closes = fixtures.selloff_then(100, 20, 0.1, up + [up[-1] - 0.1 * i for i in range(1, 60)])
        fixtures.write_symbol(self.data, "AAPL", {MON: fixtures.path_bars(fixtures.pad(closes), 50_000)})
        _, _, log = self.run_engine({"after_profit_trigger": "breakeven"})
        moved = [e for e in log if e["action"] == "stop_moved"]
        exit_ = [e for e in log if e["action"] == "exit"][0]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["new_stop"], moved[0]["entry_price"])
        self.assertAlmostEqual(exit_["pnl"], 0, places=2)

    def test_trend_filter_blocks_counter_trend_long(self):
        closes = fixtures.selloff_then(100, 20, 0.1, [98.2 + 0.1 * i for i in range(30)])
        fixtures.write_symbol(self.data, "AAPL", {MON: fixtures.path_bars(fixtures.pad(closes), 50_000)}, trend="down")
        _, broker, log = self.run_engine()
        self.assertEqual(broker.fills, [])
        skip = [e for e in log if e["action"] == "skip_signal"][0]
        self.assertIn("50 EMA above 200 EMA", skip["reasoning"])
        self.assertLess(skip["ema_fast"], skip["ema_slow"])

    def test_trend_filter_without_history_skips(self):
        closes = fixtures.selloff_then(100, 20, 0.1, [98.2 + 0.1 * i for i in range(30)])
        fixtures.write_symbol(self.data, "AAPL", {MON: fixtures.path_bars(fixtures.pad(closes), 50_000)}, trend=None)
        _, broker, log = self.run_engine()
        self.assertEqual(broker.fills, [])
        self.assertIn("not enough daily history", [e for e in log if e["action"] == "skip_signal"][0]["reasoning"])

    def test_overnight_gap_trips_kill_switch_and_flattens(self):
        day1 = fixtures.selloff_then(100, 20, 0.1, [98.1])  # enters long ~98.1, holds flat
        day2 = [94.0] * 10                                  # gaps down ~4% at the open
        fixtures.write_symbol(self.data, "AAPL", {
            MON: fixtures.path_bars(fixtures.pad(day1), 50_000),
            TUE: fixtures.path_bars(fixtures.pad(day2), 50_000)})
        engine, broker, log = self.run_engine()
        actions = [e["action"] for e in log if e["action"] in ("entry", "exit", "kill_switch")]
        # Day P&L is measured from Monday's close, so the gap trips the kill switch,
        # which flattens the position and halts trading for the day.
        self.assertEqual(actions, ["entry", "kill_switch", "exit"])
        self.assertEqual([e for e in log if e["action"] == "exit"][0]["reasoning"], "kill_switch")
        self.assertFalse([e for e in log if e["action"] == "scan" and e["ts"].startswith("2026-10-06")])
        self.assertTrue(engine.kill.tripped)
        self.assertIsNone(engine.position)

    def test_max_hold_days(self):
        day1 = fixtures.selloff_then(100, 20, 0.1, [98.1])
        days = {MON: fixtures.path_bars(fixtures.pad(day1), 50_000)}
        d = MON
        for _ in range(5):
            d += timedelta(days=1)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            days[d] = fixtures.path_bars(fixtures.pad([98.2]), 1000)
        fixtures.write_symbol(self.data, "AAPL", days)
        engine, broker, log = self.run_engine({"max_hold_days": 5})
        exit_ = [e for e in log if e["action"] == "exit"][0]
        self.assertTrue(exit_["reasoning"].startswith("max_hold_days"))

    def test_every_decision_has_timestamp_and_reasoning(self):
        closes = fixtures.selloff_then(100, 20, 0.1, [98.2 + 0.1 * i for i in range(30)])
        fixtures.write_symbol(self.data, "AAPL", {MON: fixtures.path_bars(fixtures.pad(closes), 50_000)})
        _, _, log = self.run_engine({"after_profit_trigger": "take_profit"})
        self.assertTrue(log)
        for e in log:
            self.assertIn("ts", e); self.assertIn("reasoning", e); self.assertIn("action", e)


class LoggingAndAlerts(unittest.TestCase):
    def test_secrets_are_redacted(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["UNITTEST_API_TOKEN"] = "sk-very-secret-value-123"
            try:
                lg = get_logger("redact-test", log_dir=Path(d))
                lg.info("token is sk-very-secret-value-123")
                DecisionLog("x", log_dir=Path(d)).record("t", "leak sk-very-secret-value-123")
                text = (Path(d) / "redact-test.log").read_text() + (Path(d) / "x" / "decisions.jsonl").read_text()
                self.assertNotIn("sk-very-secret-value-123", text)
                self.assertIn("[REDACTED]", text)
            finally:
                del os.environ["UNITTEST_API_TOKEN"]

    def test_alert_format_matches_spec(self):
        self.assertEqual(format_trade_alert("BUY", "AAPL", 232.10, 14.2, 3.2, 2, 1),
                         "🟢 BUY AAPL @ $232.10 | RSI(1m) 14.2 | Vol 3.2x avg | Target +2% / Stop -1%")


class DashboardAPI(unittest.TestCase):
    def setUp(self):
        from dashboard.server import Store, make_handler
        from http.server import ThreadingHTTPServer
        self.tmp = tempfile.TemporaryDirectory()
        manifest = Path(self.tmp.name) / "manifest.json"
        manifest.write_text(json.dumps({"agents": [{"name": "trader"}]}))
        store = Store(Path(self.tmp.name) / "data", manifest)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, get_logger("dash-test", Path(self.tmp.name))))
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        os.environ["DASHBOARD_TOKEN"] = "test-dash-token"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.tmp.cleanup()
        del os.environ["DASHBOARD_TOKEN"]

    def req(self, path, body=None, token="test-dash-token"):
        r = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                   headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                   method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_status_roundtrip_and_shape(self):
        code, agents = self.req("/api/agents")
        self.assertEqual(agents[0]["name"], "trader")
        self.assertEqual(agents[0]["status"], "idle")
        status = build_status("trader", "watching", "scanning", 3, 12.5)
        self.assertEqual(self.req("/api/agents/trader/status", status)[0], 200)
        code, got = self.req("/api/agents/trader/status")
        self.assertEqual(got, status)
        self.assertEqual(sorted(got), sorted(["name", "status", "current_task", "tasks_today", "pnl", "last_updated"]))
        code, dash = self.req("/api/dashboard")
        self.assertEqual(set(dash), {"agents", "outbox", "heartbeat"})
        self.assertEqual(dash["heartbeat"][0]["events"], 1)

    def test_rejects_bad_shape_unknown_agent_and_bad_token(self):
        status = build_status("trader", "x", "y", 0, 0)
        self.assertEqual(self.req("/api/agents/trader/status", {**status, "extra": 1})[0], 400)
        self.assertEqual(self.req("/api/agents/ghost/status", {**status, "name": "ghost"})[0], 404)
        self.assertEqual(self.req("/api/agents/trader/status", status, token="wrong")[0], 401)

    def test_outbox(self):
        self.assertEqual(self.req("/api/outbox", {"agent": "trader", "title": "Closed AAPL +$20"})[0], 201)
        code, items = self.req("/api/outbox")
        self.assertEqual(items[0]["title"], "Closed AAPL +$20")


if __name__ == "__main__":
    unittest.main()
