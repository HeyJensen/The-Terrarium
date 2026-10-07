#!/usr/bin/env python3
"""The Terrarium Trader: one command to start the live console.

    python terrarium.py            scan Yahoo Finance every minute and show every requirement, dry run
    python terrarium.py --report   also publish status and signals to the local dashboard API

Python 3.11+, no installs. Orders stay simulated unless you deliberately wire
and arm a live broker (see README.md). Not financial advice.
"""
import sys

if sys.version_info < (3, 11):
    sys.exit("The Terrarium needs Python 3.11 or newer.")

from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.trader.run import make_yahoo_feed, task_trade  # noqa: E402
from shared.config import load_settings  # noqa: E402
from shared.logger import get_logger  # noqa: E402
from shared.market_calendar import to_et  # noqa: E402

DISCLAIMER = """
THE TERRARIUM · Trader
This software is experimental and is not financial advice. Trading can lose
money, including more than you expect from gaps, halts, slow data, or bugs.
Market data comes from Yahoo Finance (unofficial, personal use only, can be
delayed or blocked). Orders are SIMULATED unless you deliberately enable a
live broker. You are responsible for every trade made with your account.
"""


def main() -> int:
    print(DISCLAIMER)
    settings = load_settings()
    logger = get_logger("trader")
    feed = make_yahoo_feed(settings, logger)
    now = datetime.now(timezone.utc)
    missing = [s for s in feed.universe if feed._load_daily_cache(s, to_et(now).date()) is None]
    if missing:
        print(f"Loading a year of daily prices for {len(missing)} stocks (once a day, a few minutes)…")
        feed.prefetch_daily(now)
    return task_trade(settings, report="--report" in sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
