"""Hosted scanner: runs the Trader's strategy on Yahoo Finance data with no
broker attached and publishes what it sees as one JSON file the website reads.

Nobody's computer has to be on for the site to show live signals. People who
run the Trader console at home place their own trades; this only reports.

  python website/scanner.py --out site-data/dashboard.json
      run the minute loop until the market closes (or --max-minutes runs out),
      rewriting the JSON after every minute
  python website/scanner.py --out ... --publish-cmd "bash website/publish.sh"
      also run a command after each write (the GitHub workflow pushes the file)
  python website/scanner.py --replay DIR --out ...
      run over recorded CSV bars instead of Yahoo (for testing, no network)

The JSON has the same shape as GET /api/dashboard: {agents, outbox, heartbeat,
signals} plus `scan` (what the latest minute checked) and `updated_at`.
State (signal ledger, simulated bot, Yahoo cache) lives in --state-dir so a
later run picks up where the last one stopped.
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.config import ROOT, load_settings  # noqa: E402
from shared.logger import DecisionLog, get_logger  # noqa: E402
from shared.market_calendar import is_regular_hours, is_trading_day, to_et  # noqa: E402
from shared.notifications import Notifier  # noqa: E402

from agents.trader.broker import SimBroker  # noqa: E402
from agents.trader.data_feed import ReplayFeed  # noqa: E402
from agents.trader.engine import TraderEngine  # noqa: E402
from agents.trader.yahoo_feed import YahooFeed  # noqa: E402

AGENT = "trader"
SCAN_FIELDS = ("symbol", "rel_vol", "rsi", "price", "signal", "checks")


class Snapshot:
    """Collects what the website needs while the engine runs."""

    def __init__(self, path: Path):
        self.path = path
        old = json.loads(path.read_text()) if path.exists() else {}
        self.status = next((a for a in old.get("agents", []) if a.get("name") == AGENT), None)
        self.outbox: list[dict] = old.get("outbox", [])
        self.heartbeat: dict[str, int] = {h["minute"]: h["events"] for h in old.get("heartbeat", [])}
        self.closed_seen = {o.get("id") for o in self.outbox}

    def report(self, status, task, tasks_today, pnl):
        self.status = {"name": AGENT, "status": status, "current_task": task, "tasks_today": int(tasks_today),
                       "pnl": round(float(pnl), 2), "last_updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.beat()

    def beat(self):
        minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        key = minute.isoformat()
        self.heartbeat[key] = self.heartbeat.get(key, 0) + 1
        cutoff = (minute - timedelta(hours=24)).isoformat()
        for k in [k for k in self.heartbeat if k < cutoff]:
            del self.heartbeat[k]

    def note_closed_signals(self, signals: list[dict]):
        for s in signals:
            oid = f"closed-{s['id']}"
            if s["status"] == "closed" and oid not in self.closed_seen:
                self.closed_seen.add(oid)
                side = "buy" if s["side"] == "buy" else "short"
                self.outbox.insert(0, {"id": oid, "agent": AGENT, "ts": s["exit_ts"] or s["ts"],
                                       "title": f"{s['symbol']} {side} signal closed, {s['exit_reason']}",
                                       "detail": f"Signal {s['price']:.2f}, exit {s['exit_price']:.2f}. "
                                                 f"{s['pnl_pct']:+.2f}% ({'+' if s['pnl_pct'] >= 0 else '-'}${abs(s['pnl_pct']) * 10:.2f} per $1,000)."})
        del self.outbox[200:]

    def write(self, engine) -> None:
        signals = engine.signals.recent(300)
        self.note_closed_signals(signals)
        scan = None
        if engine.last_scan:
            scan = {"ts": engine.last_scan["ts"], "universe": engine.last_scan["universe"],
                    "symbols_with_data": engine.last_scan["symbols_with_data"],
                    "rows": [{k: r.get(k) for k in SCAN_FIELDS} for r in engine.last_scan["rows"]]}
        out = {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "hosted-scanner",
            "agents": [self.status] if self.status else [],
            "outbox": self.outbox,
            "heartbeat": [{"minute": k, "events": v} for k, v in sorted(self.heartbeat.items())],
            "signals": signals,
            "scan": scan,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, default=str))
        tmp.replace(self.path)


def build(settings, feed, universe, state_dir: Path, snap: Snapshot, logger):
    cfg = dict(settings["trader"], order_routing="dry_run")  # the hosted scanner never routes orders
    sim_path = state_dir / "sim_broker.json"
    broker = SimBroker.load_or_new(sim_path, cfg["account_size_usd"], cfg["mode"], cfg.get("sim_slippage_bps", 5))

    def bar_range(symbol, now):
        bars = feed.minute_bars(symbol, now)
        return (bars[-1].low, bars[-1].high) if bars else None
    broker.price_source = bar_range
    engine = TraderEngine(cfg, broker, feed, universe, state_dir, DecisionLog(AGENT, log_dir=state_dir / "logs"),
                          logger, Notifier({"notifications": {"telegram_enabled": False}}, logger),
                          reporter=snap.report)
    return engine, broker, sim_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Terrarium hosted scanner")
    ap.add_argument("--out", type=Path, required=True, help="JSON file the website reads")
    ap.add_argument("--state-dir", type=Path, default=ROOT / "state" / "scanner")
    ap.add_argument("--max-minutes", type=int, default=345, help="stop after this long (GitHub jobs end at 6 h)")
    ap.add_argument("--publish-cmd", default=None, help="shell command to run after each write")
    ap.add_argument("--replay", type=Path, default=None, help="CSV bar directory instead of Yahoo (testing)")
    ap.add_argument("--universe", default="config/universe_sp100.json", help="symbol list to scan (Nathan: S&P 100)")
    args = ap.parse_args(argv)

    settings = load_settings()
    cfg = settings["trader"]
    logger = get_logger("scanner")
    state_dir = args.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    universe = json.loads((ROOT / args.universe).read_text())["symbols"]
    snap = Snapshot(args.out)

    def publish():
        if args.publish_cmd:
            r = subprocess.run(args.publish_cmd, shell=True)
            if r.returncode:
                logger.warning(f"publish command exited {r.returncode}")

    if args.replay:
        feed = ReplayFeed(args.replay)
        universe = [s for s in universe if s in set(feed.symbols())] or feed.symbols()
        engine, broker, sim_path = build(settings, feed, universe, state_dir, snap, logger)
        for now in feed.timeline():
            engine.step(now)
        broker.save(sim_path)
        snap.write(engine)
        publish()
        print(json.dumps({"signals": len(engine.signals.entries), "out": str(args.out)}))
        return 0

    feed = YahooFeed(universe, state_dir / "yahoo-cache", logger,
                     max_requests_per_minute=int(cfg.get("yahoo_max_requests_per_minute", 50)))
    ok, failed = feed.prefetch_daily(datetime.now(timezone.utc))  # cached per day; cheap after the first run
    logger.info(f"daily history ready for {ok} symbols, {failed} failed")
    engine, broker, sim_path = build(settings, feed, universe, state_dir, snap, logger)
    started = time.time()
    seen_open = False
    et = to_et(datetime.now(timezone.utc))
    in_window = is_trading_day(et.date()) and (9, 0) <= (et.hour, et.minute) < (16, 30)
    if not in_window:  # outside market hours: refresh the snapshot once and stop
        engine.step(datetime.now(timezone.utc))
        broker.save(sim_path)
        snap.write(engine)
        publish()
        logger.info("market closed; wrote one snapshot and stopped")
        return 0
    logger.info("hosted scanner running on Yahoo Finance data; it reports only and places no orders")
    while time.time() - started < args.max_minutes * 60:
        now = datetime.now(timezone.utc)
        open_now = is_regular_hours(now)
        seen_open = seen_open or open_now
        engine.step(now)
        broker.save(sim_path)
        snap.write(engine)
        publish()
        if seen_open and not open_now:
            logger.info("market closed; final snapshot written")
            break
        interval = int(cfg.get("loop_interval_seconds", 60))
        time.sleep(interval - (time.time() % interval) + 3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
