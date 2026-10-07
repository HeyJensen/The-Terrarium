"""Content agent: blog post drafts that earn through AdSense and Amazon affiliate links.

  python agents/content/run.py --status
  python agents/content/run.py --task check
  python agents/content/run.py --task draft   trends + niches on the board -> blog post drafts for approval

Each draft is Markdown with:
- the affiliate disclosure at the top (FTC rule and Amazon Associates terms),
- affiliate links as [[amazon: ...]] placeholders, since product links come
  from Amazon's Creators API only after 10 sales in 30 days; until then a
  human picks products and pastes SiteStripe links,
- [[adsense]] slot markers where ads may go,
- an [[etsy]] slot to cross-sell our own listings.

Google ranks down mass-produced unedited AI pages, and AdSense rejects thin
sites, so every draft is a starting point for a human edit, not a final post.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.config import ROOT  # noqa: E402
from shared.logger import DecisionLog, get_logger  # noqa: E402

from agents.common.approvals import ApprovalQueue  # noqa: E402
from agents.common.board import Board  # noqa: E402
from agents.common.bot import ip_flags, load_config, publishing_problem, report  # noqa: E402
from agents.common.brain import get_brain  # noqa: E402

AGENT = "content"

DISCLOSURE = ("*This post contains affiliate links. As an Amazon Associate we earn from qualifying "
              "purchases, at no extra cost to you.*")


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:70]


def build_post(topic: str, keywords: list[str], brain, cfg) -> dict:
    title = f"{topic.title()}: {cfg['title_pattern']}"
    outline = brain.blog_outline(topic, keywords)
    body = [f"# {title}", "", DISCLOSURE, ""]
    for i, heading in enumerate(outline):
        body += [f"## {heading}", "", "[EDIT: write this section in your own words]", ""]
        if i == 2:
            body += [f"- [[amazon: search \"{topic}\" — pick a product you'd actually recommend]]"
                     for _ in range(cfg["affiliate_slots"])] + [""]
        if i in (0, 3):
            body += ["[[adsense]]", ""]
    body += ["## From our shop", "", f"[[etsy: our {topic} listings, once approved and live]]", ""]
    return {
        "title": title,
        "slug": slugify(topic),
        "meta_description": f"[EDIT: 150 characters on {topic}]",
        "keywords": keywords,
        "outline": outline,
        "markdown": "\n".join(body),
        "target_words": cfg["target_words"],
    }


def draft(cfg, board: Board, queue: ApprovalQueue, decisions: DecisionLog, brain) -> list[dict]:
    niches, trends = board.unread("niches", AGENT), board.unread("trends", AGENT)
    # Niches first: they have search data behind them. Trends fill the remaining slots.
    ideas = [(n["niche"], [n["niche"]], n["id"]) for n in niches]
    ideas += [(t["trend"], [t["trend"]], t["id"]) for t in trends]
    existing = {i["draft"]["slug"] for i in queue.list(kind="blog_post")}
    submitted = []
    for topic, keywords, src in ideas:
        if len(submitted) >= cfg["posts_per_run"]:
            break
        post = build_post(topic, keywords, brain, cfg)
        if post["slug"] in existing:
            decisions.record("skip_post", "a draft on this topic already exists", topic=topic)
            continue
        existing.add(post["slug"])
        item = queue.submit(AGENT, "blog_post", post["title"], post, flags=ip_flags(topic), source_ids=[src])
        decisions.record("draft_post", "from board", topic=topic, approval_id=item["id"])
        submitted.append(item)
    board.ack("niches", AGENT, len(niches))
    board.ack("trends", AGENT, len(trends))
    return submitted


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Terrarium content agent")
    ap.add_argument("--task", choices=["check", "draft"])
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--report", action="store_true", help="post status to the dashboard API")
    args = ap.parse_args(argv)
    cfg = load_config(AGENT)
    logger = get_logger(AGENT)
    problem = publishing_problem(cfg)
    if args.status:
        q = ApprovalQueue()
        print(json.dumps({"name": AGENT, "pending": len(q.list(status="pending", agent=AGENT)),
                          "approved": len(q.list(status="approved", agent=AGENT))}, indent=2))
        return 0
    if args.task == "check":
        print(json.dumps({"config": cfg, "publishing_problem": problem}, indent=2))
        return 0 if problem is None else 1
    if problem:
        logger.error(f"refusing to run: {problem}")
        return 2
    if args.task == "draft":
        items = draft(cfg, Board(), ApprovalQueue(), DecisionLog(AGENT), get_brain(cfg["brain"]))
        logger.info(f"{len(items)} blog drafts waiting for approval")
        if args.report:
            report(AGENT, logger, "idle", f"drafted {len(items)} posts", len(items))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
