"""Trader agent entrypoint (standard agent CLI).

  python agents/trader/run.py --status             print current state as JSON
  python agents/trader/run.py --task check         validate config + guardrails, show what would run
  python agents/trader/run.py --task replay --data DIR [--state-dir DIR]
                                                   run the full engine over CSV bars with the simulator
  python agents/trader/run.py --task prefetch      cache a year of daily bars from Yahoo (run before the open)
  python agents/trader/run.py --task trade         the minute loop on live Yahoo data; dry run only for now
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.config import ROOT, env, load_settings  # noqa: E402
from shared.dashboard_client import build_status, post_signals, post_status  # noqa: E402
from shared.logger import DecisionLog, get_logger  # noqa: E402
from shared.notifications import Notifier  # noqa: E402
from shared.risk import effective_limits  # noqa: E402

from agents.trader.broker import SimBroker  # noqa: E402
from agents.trader.data_feed import ReplayFeed  # noqa: E402
from agents.trader.yahoo_feed import YahooFeed  # noqa: E402
from agents.trader.console import render as render_console  # noqa: E402
from agents.trader.engine import TraderEngine  # noqa: E402

AGENT = "trader"
LIVE_ARM_PHRASE = "yes-real-money"
DEFAULT_STATE = ROOT / "state" / AGENT


def load_universe(cfg: dict) -> list[str]:
    return json.loads((ROOT / cfg["universe_file"]).read_text())["symbols"]


def make_reporter(logger):
    def report(status, task, tasks_today, pnl):
        post_status(build_status(AGENT, status, task, tasks_today, pnl), logger)
    return report


def build_engine(settings, broker, feed, universe, state_dir, logger, report=False, decisions=None, sleep=None):
    sink = (lambda signals: post_signals(AGENT, signals, logger)) if report else None
    return TraderEngine(settings["trader"], broker, feed, universe, state_dir, decisions or DecisionLog(AGENT),
                        logger, Notifier(settings, logger), reporter=make_reporter(logger) if report else None,
                        signal_sink=sink, sleep=sleep)


def latest_bar_range(feed):
    """For the simulator: did price trade through a resting limit in the latest bar?"""
    def source(symbol, now):
        bars = feed.minute_bars(symbol, now)
        return (bars[-1].low, bars[-1].high) if bars else None
    return source


def routing_problem(cfg: dict) -> str | None:
    if cfg["order_routing"] == "dry_run":
        return None
    if cfg["order_routing"] != "live":
        return f"order_routing must be 'dry_run' or 'live', got {cfg['order_routing']!r}"
    if env("TRADER_LIVE_ARMED") != LIVE_ARM_PHRASE:
        return f"order_routing is 'live' but TRADER_LIVE_ARMED is not '{LIVE_ARM_PHRASE}'"
    return None


def task_check(settings) -> int:
    cfg = settings["trader"]
    limits = effective_limits(cfg)
    problem = routing_problem(cfg)
    print(json.dumps({
        "limits": limits,
        "order_routing": cfg["order_routing"],
        "live_orders_possible": cfg["order_routing"] == "live" and problem is None,
        "routing_problem": problem,
        "phase": cfg.get("phase"),
        "universe_size": len(load_universe(cfg)),
        "long_only": limits["mode"] == "cash",
    }, indent=2))
    return 0 if problem is None else 1


def task_replay(settings, data_dir: Path, state_dir: Path, report: bool) -> int:
    cfg = settings["trader"]
    logger = get_logger(AGENT)
    feed = ReplayFeed(data_dir)
    broker = SimBroker(cfg["account_size_usd"], account_type=cfg["mode"], slippage_bps=cfg.get("sim_slippage_bps", 5),
                       price_source=latest_bar_range(feed))
    universe = [s for s in load_universe(cfg) if s in set(feed.symbols())] or feed.symbols()
    # Replay keeps its decisions next to its own state so it never mixes with the live audit trail.
    engine = build_engine(settings, broker, feed, universe, state_dir, logger, report,
                          decisions=DecisionLog(AGENT, log_dir=Path(state_dir) / "logs"))
    for now in feed.timeline():
        engine.step(now)
    final_marks = {}
    if engine.position:
        bars = feed.minute_bars(engine.position.symbol, feed.timeline()[-1])
        final_marks = {engine.position.symbol: bars[-1].close} if bars else {}
    acct = broker.account(final_marks)
    print(json.dumps({"fills": len(broker.fills), "ending_equity": round(acct.equity, 2),
                      "status": engine.status(), "decision_log": str(engine.decisions.path)}, indent=2, default=str))
    return 0


def make_yahoo_feed(settings, logger) -> YahooFeed:
    cfg = settings["trader"]
    return YahooFeed(load_universe(cfg), ROOT / "state" / "yahoo-cache", logger,
                     max_requests_per_minute=int(cfg.get("yahoo_max_requests_per_minute", 50)))


def task_prefetch(settings) -> int:
    logger = get_logger(AGENT)
    feed = make_yahoo_feed(settings, logger)
    ok, failed = feed.prefetch_daily(datetime.now(timezone.utc))
    logger.info(f"daily history cached for {ok} symbols, {failed} failed")
    return 0 if failed == 0 else 1


def task_trade(settings, report: bool, console: bool = True) -> int:
    """The live loop: real Yahoo data every minute.

    dry_run: orders go to the simulator (no money moves); its cash and
    positions persist in state/trader/ alongside the engine's.
    live:    refused until the Robinhood MCP broker is implemented and approved.
    """
    cfg = settings["trader"]
    logger = get_logger(AGENT)
    problem = routing_problem(cfg)
    if problem:
        logger.error(f"refusing to start: {problem}")
        return 2
    if cfg["order_routing"] == "live":
        logger.error("refusing to start: the Robinhood MCP broker is not implemented yet "
                     "(see docs/robinhood-oauth-setup.md). Use order_routing dry_run.")
        return 3
    DEFAULT_STATE.mkdir(parents=True, exist_ok=True)
    sim_path = DEFAULT_STATE / "sim_broker.json"
    broker = SimBroker.load_or_new(sim_path, cfg["account_size_usd"], cfg["mode"], cfg.get("sim_slippage_bps", 5))
    feed = make_yahoo_feed(settings, logger)
    broker.price_source = latest_bar_range(feed)
    engine = build_engine(settings, broker, feed, load_universe(cfg), DEFAULT_STATE, logger, report, sleep=time.sleep)
    logger.info("trader running in DRY RUN on Yahoo Finance data; no orders reach any broker. Ctrl+C to stop.")
    interval = int(cfg.get("loop_interval_seconds", 60))
    try:
        while True:
            now = datetime.now(timezone.utc)
            engine.step(now)
            broker.save(sim_path)
            if console:
                print("\033[2J\033[H" + render_console(engine, now, cfg["order_routing"]), flush=True)
            # Wake a few seconds after each minute boundary so the last bar has closed.
            time.sleep(interval - (time.time() % interval) + 3)
    except KeyboardInterrupt:
        broker.save(sim_path)
        logger.info("stopped by user")
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Terrarium trader agent")
    ap.add_argument("--task", choices=["check", "prefetch", "replay", "trade"])
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--data", type=Path, help="replay data directory")
    ap.add_argument("--state-dir", type=Path, default=None)
    ap.add_argument("--report", action="store_true", help="post status and signals to the dashboard API")
    ap.add_argument("--no-console", action="store_true", help="log only, no live console view")
    args = ap.parse_args(argv)
    settings = load_settings()

    if args.status:
        state_dir = args.state_dir or DEFAULT_STATE
        out = {"name": AGENT}
        for f in ("position", "ledger", "killswitch"):
            p = state_dir / f"{f}.json"
            out[f] = json.loads(p.read_text()) if p.exists() else None
        out["order_routing"] = settings["trader"]["order_routing"]
        print(json.dumps(out, indent=2))
        return 0
    if args.task == "check":
        return task_check(settings)
    if args.task == "replay":
        if not args.data:
            ap.error("--task replay needs --data DIR")
        return task_replay(settings, args.data, args.state_dir or ROOT / "state" / "replay", args.report)
    if args.task == "prefetch":
        return task_prefetch(settings)
    if args.task == "trade":
        return task_trade(settings, args.report, console=not args.no_console)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
