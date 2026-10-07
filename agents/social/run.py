"""Social agent: spots trends for the other bots and drafts promo posts.

  python agents/social/run.py --status
  python agents/social/run.py --task check     show config and the draft-only lock
  python agents/social/run.py --task scan      trend files in state/inbox/social/ -> the trends board
  python agents/social/run.py --task promote   approved listings and blog posts -> draft social posts for approval
  python agents/social/run.py --task channels  check BUFFER_API_KEY and list the channels Buffer has connected
  python agents/social/run.py --task publish   dry run: show which approved posts would go to Buffer
  python agents/social/run.py --task publish --live
                                               queue them in Buffer. Needs publishing_mode "buffer" in
                                               config/settings.json and BUFFER_API_KEY in the environment.

Only approved posts with no [EDIT] placeholders left are ever sent.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.config import ROOT, env  # noqa: E402
from shared.logger import DecisionLog, get_logger  # noqa: E402

from agents.common.approvals import ApprovalQueue  # noqa: E402
from agents.common.board import Board  # noqa: E402
from agents.common.bot import load_config, num, publishing_problem, read_inbox, report  # noqa: E402
from agents.common.brain import get_brain  # noqa: E402
from agents.social.buffer import BufferClient, publish  # noqa: E402

AGENT = "social"
STATE = ROOT / "state" / AGENT

# Hard text limits per platform, so a draft is never rejected for length.
PLATFORM_LIMITS = {"pinterest": 500, "instagram": 2200, "x": 280, "facebook": 2000, "tiktok": 2200}
PROMOTABLE = ("etsy_listing", "blog_post")
PUBLISHING_MODES = ("draft_only", "buffer")


def trend_score(row: dict) -> float:
    """0-100. A row's own score, lifted by how fast it's growing."""
    base = num(row.get("score") or row.get("interest"), 50)
    growth = num(row.get("growth_pct") or row.get("growth") or row.get("velocity"), 0)
    return round(min(100.0, base * (1 + max(growth, 0) / 200)), 1)


def scan(cfg, board: Board, decisions: DecisionLog, rows: list[dict], now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=cfg["dedupe_days"])
    recent = {t["trend"].lower() for t in board.read("trends")
              if datetime.fromisoformat(t["posted_at"]) >= cutoff}
    posted = []
    for row in sorted(rows, key=trend_score, reverse=True):
        topic = (row.get("topic") or row.get("trend") or row.get("keyword") or "").strip()
        score = trend_score(row)
        if not topic:
            continue
        if topic.lower() in recent:
            decisions.record("skip_trend", "already on the board this week", trend=topic)
            continue
        if score < cfg["min_trend_score"]:
            decisions.record("skip_trend", f"score {score} under {cfg['min_trend_score']}", trend=topic)
            continue
        recent.add(topic.lower())
        item = board.post("trends", AGENT, {"trend": topic, "score": score, "platform": row.get("source") or row.get("platform") or "manual",
                                            "audience": row.get("audience") or "", "notes": row.get("notes") or ""})
        decisions.record("post_trend", f"score {score}", trend=topic, board_id=item["id"])
        posted.append(item)
    return posted


def promote(cfg, queue: ApprovalQueue, decisions: DecisionLog, brain, state_dir: Path = STATE) -> list[dict]:
    state_dir.mkdir(parents=True, exist_ok=True)
    done_path = state_dir / "promoted.json"
    done = set(json.loads(done_path.read_text())) if done_path.exists() else set()
    drafts = []
    for item in queue.list(status="approved"):
        if item["kind"] not in PROMOTABLE or item["id"] in done:
            continue
        for platform in cfg["platforms"]:
            hook = "New in the shop:" if item["kind"] == "etsy_listing" else "New on the blog:"
            text = brain.social_post(platform, item["title"], hook)[:PLATFORM_LIMITS.get(platform, 280)]
            drafts.append(queue.submit(AGENT, "social_post", f"{platform}: {item['title']}",
                                       {"platform": platform, "text": text, "link": "[EDIT: listing or post URL once live]",
                                        "image": "use the listing mockup" if item["kind"] == "etsy_listing" else "post header image",
                                        "promotes": item["id"]}, source_ids=[item["id"]]))
        decisions.record("draft_promo", f"{len(cfg['platforms'])} platform drafts", promotes=item["id"])
        done.add(item["id"])
    done_path.write_text(json.dumps(sorted(done)))
    return drafts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Terrarium social agent")
    ap.add_argument("--task", choices=["check", "scan", "promote", "channels", "publish"])
    ap.add_argument("--live", action="store_true", help="with --task publish: really queue posts in Buffer")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--report", action="store_true", help="post status to the dashboard API")
    args = ap.parse_args(argv)
    cfg = load_config(AGENT)
    logger = get_logger(AGENT)
    problem = publishing_problem(cfg, PUBLISHING_MODES)
    if args.status:
        print(json.dumps({"name": AGENT, "publishing_mode": cfg["publishing_mode"], "trends_on_board": len(Board().read("trends")),
                          "drafts_pending": len(ApprovalQueue().list(status="pending", kind="social_post"))}, indent=2))
        return 0
    if args.task == "check":
        print(json.dumps({"config": cfg, "publishing_problem": problem, "inbox": str(ROOT / "state" / "inbox" / AGENT)}, indent=2))
        return 0 if problem is None else 1
    if problem:
        logger.error(f"refusing to run: {problem}")
        return 2
    decisions = DecisionLog(AGENT)
    if args.task == "scan":
        posted = scan(cfg, Board(), decisions, read_inbox(AGENT))
        logger.info(f"{len(posted)} trends posted to the board")
        if args.report:
            report(AGENT, logger, "idle", f"posted {len(posted)} trends", len(posted))
        return 0
    if args.task == "promote":
        drafts = promote(cfg, ApprovalQueue(), decisions, get_brain(cfg["brain"]))
        logger.info(f"{len(drafts)} social posts drafted for approval")
        if args.report:
            report(AGENT, logger, "idle", f"drafted {len(drafts)} posts", len(drafts))
        return 0
    if args.task == "channels":
        if not env("BUFFER_API_KEY"):
            logger.error("BUFFER_API_KEY is not set (config/.env)")
            return 2
        for c in BufferClient(env("BUFFER_API_KEY")).channels():
            print(f"{c['service']:<12} {c['name']}")
        return 0
    if args.task == "publish":
        if args.live and cfg["publishing_mode"] != "buffer":
            logger.error('refusing to publish: set "social": {"publishing_mode": "buffer"} in config/settings.json first')
            return 2
        if args.live and not env("BUFFER_API_KEY"):
            logger.error("refusing to publish: BUFFER_API_KEY is not set (config/.env)")
            return 2
        client = BufferClient(env("BUFFER_API_KEY")) if args.live else None
        results = publish(cfg, ApprovalQueue(), decisions, client, STATE, dry_run=not args.live)
        for r in results:
            print(f"{r['id']:<26} {r['platform']:<10} {r['outcome']}")
        queued = sum(r["outcome"].startswith("queued") for r in results)
        if args.report:
            report(AGENT, logger, "idle", f"queued {queued} posts in Buffer" if args.live else "publish dry run", queued)
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
