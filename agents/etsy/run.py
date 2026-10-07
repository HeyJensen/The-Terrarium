"""Etsy research agent: finds niches worth making products for.

  python agents/etsy/run.py --status
  python agents/etsy/run.py --task check
  python agents/etsy/run.py --task scan     keyword exports + trends -> scored niches on the board

Where the numbers come from: Etsy's API terms forbid scraping the site and
using Etsy data for analytics without written permission, so this bot never
touches etsy.com. It reads keyword exports Nathan downloads from a research
tool (eRank, EverBee, Marmalead) into state/inbox/etsy/. Trends with no
keyword data yet go to state/etsy/keywords_to_check.txt for the next export.
"""
import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.config import ROOT  # noqa: E402
from shared.logger import DecisionLog, get_logger  # noqa: E402

from agents.common.board import Board  # noqa: E402
from agents.common.bot import load_config, num, publishing_problem, read_inbox, report  # noqa: E402

AGENT = "etsy"
STATE = ROOT / "state" / AGENT

# Column names used by common research-tool exports, after snake_casing.
KEYWORD_COLS = ("keyword", "keywords", "search_term", "tag")
SEARCH_COLS = ("avg_searches", "searches", "monthly_searches", "search_volume", "etsy_searches")
COMPETITION_COLS = ("etsy_competition", "competition", "listings", "results", "competing_listings")
PRICE_COLS = ("avg_price", "average_price", "price")


def pick(row: dict, cols) -> str | None:
    return next((row[c] for c in cols if row.get(c) not in (None, "")), None)


def opportunity(searches: float, competition: float) -> float:
    """Searches per unit of competition, log-damped so huge keywords don't swamp the list."""
    return round(math.log10(searches + 1) * 1000 / math.log10(competition + 10), 1)


def scan(cfg, board: Board, decisions: DecisionLog, rows: list[dict], state_dir: Path = STATE) -> list[dict]:
    trends = board.unread("trends", AGENT)
    trend_words = {t["trend"].lower(): t for t in trends}
    scored = []
    for row in rows:
        kw = (pick(row, KEYWORD_COLS) or "").strip().lower()
        if not kw:
            continue
        searches, competition = num(pick(row, SEARCH_COLS)), num(pick(row, COMPETITION_COLS))
        price = num(pick(row, PRICE_COLS))
        reasons = []
        if searches < cfg["min_monthly_searches"]:
            reasons.append(f"searches {searches:.0f} < {cfg['min_monthly_searches']}")
        if competition > cfg["max_competition"]:
            reasons.append(f"competition {competition:.0f} > {cfg['max_competition']}")
        if price and price < cfg["min_avg_price"]:
            reasons.append(f"avg price {price:.2f} < {cfg['min_avg_price']}")
        if reasons:
            decisions.record("skip_niche", "; ".join(reasons), keyword=kw)
            continue
        score = opportunity(searches, competition)
        trend = next((t for word, t in trend_words.items() if word in kw or kw in word), None)
        if trend:
            score = round(score * cfg["trend_boost"], 1)
        scored.append({"niche": kw, "score": score, "searches": searches, "competition": competition,
                       "avg_price": price or None, "trend_id": trend["id"] if trend else None})
    scored.sort(key=lambda n: n["score"], reverse=True)
    posted = []
    for n in scored[:cfg["niches_per_scan"]]:
        item = board.post("niches", AGENT, n)
        decisions.record("post_niche", f"opportunity {n['score']}", keyword=n["niche"], board_id=item["id"])
        posted.append(item)
    # Trends no export covered yet: Nathan looks these up in the research tool next time.
    covered = {n["niche"] for n in scored}
    todo = [t["trend"] for t in trends if not any(t["trend"].lower() in c or c in t["trend"].lower() for c in covered)]
    if todo:
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_dir / "keywords_to_check.txt", "a") as f:
            f.writelines(t + "\n" for t in todo)
    board.ack("trends", AGENT, len(trends))
    return posted


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Terrarium Etsy research agent")
    ap.add_argument("--task", choices=["check", "scan"])
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--report", action="store_true", help="post status to the dashboard API")
    args = ap.parse_args(argv)
    cfg = load_config(AGENT)
    logger = get_logger(AGENT)
    problem = publishing_problem(cfg)
    if args.status:
        todo = STATE / "keywords_to_check.txt"
        print(json.dumps({"name": AGENT, "niches_on_board": len(Board().read("niches")),
                          "keywords_to_check": todo.read_text().split("\n")[:-1] if todo.exists() else []}, indent=2))
        return 0
    if args.task == "check":
        print(json.dumps({"config": cfg, "publishing_problem": problem, "inbox": str(ROOT / "state" / "inbox" / AGENT)}, indent=2))
        return 0 if problem is None else 1
    if problem:
        logger.error(f"refusing to run: {problem}")
        return 2
    if args.task == "scan":
        posted = scan(cfg, Board(), DecisionLog(AGENT), read_inbox(AGENT))
        logger.info(f"{len(posted)} niches posted to the board")
        if args.report:
            report(AGENT, logger, "idle", f"posted {len(posted)} niches", len(posted))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
