"""Trader agent entrypoint (standard agent CLI).

  python agents/trader/run.py --status             print current state as JSON
  python agents/trader/run.py --task check         validate config + guardrails, show what would run
  python agents/trader/run.py --task replay --data DIR [--state-dir DIR]
                                                   run the full engine over CSV bars with the simulator
  python agents/trader/run.py --task trade         the live loop (refuses until a live data feed and
                                                   broker are wired and armed; see docs/)
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.config import ROOT, env, load_settings  # noqa: E402
from shared.dashboard_client import build_status, post_status  # noqa: E402
from shared.logger import DecisionLog, get_logger  # noqa: E402
from shared.notifications import Notifier  # noqa: E402
from shared.risk import effective_limits  # noqa: E402

from agents.trader.broker import RobinhoodMCPBroker, SimBroker  # noqa: E402
from agents.trader.data_feed import ReplayFeed  # noqa: E402
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


def build_engine(settings, broker, feed, universe, state_dir, logger, report=False, decisions=None):
    return TraderEngine(settings["trader"], broker, feed, universe, state_dir, decisions or DecisionLog(AGENT),
                        logger, Notifier(settings, logger), reporter=make_reporter(logger) if report else None)


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
    broker = SimBroker(cfg["account_size_usd"], account_type=cfg["mode"], slippage_bps=cfg.get("sim_slippage_bps", 5))
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


def task_trade(settings, report: bool) -> int:
    cfg = settings["trader"]
    logger = get_logger(AGENT)
    problem = routing_problem(cfg)
    if problem:
        logger.error(f"refusing to start: {problem}")
        return 2
    # Neither a live 1-minute data feed nor the live broker is wired yet. Both need decisions
    # from the human (data source, MCP client dependency). Stop here rather than guess.
    logger.error("live loop not available yet: no live 1-minute data feed is configured and the "
                 "Robinhood MCP broker is not implemented. See docs/robinhood-oauth-setup.md.")
    if cfg["order_routing"] == "live":
        RobinhoodMCPBroker()  # documents the intended route; every call refuses
    return 3


def run_loop(engine, interval_s: int = 60) -> None:  # used once a live feed exists
    while True:
        engine.step(datetime.now(timezone.utc))
        time.sleep(interval_s - (time.time() % interval_s) + 2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Terrarium trader agent")
    ap.add_argument("--task", choices=["check", "replay", "trade"])
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--data", type=Path, help="replay data directory")
    ap.add_argument("--state-dir", type=Path, default=None)
    ap.add_argument("--report", action="store_true", help="post status to the dashboard API")
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
    if args.task == "trade":
        return task_trade(settings, args.report)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
