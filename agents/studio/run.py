"""Studio agent: turns niches into original product drafts for the approval queue.

  python agents/studio/run.py --status
  python agents/studio/run.py --task check
  python agents/studio/run.py --task draft    niches on the board -> t-shirt and printable listing drafts
  python agents/studio/run.py --task export   approved listings -> state/studio/etsy_sheets/<date>.md to copy into
                                              Printify (t-shirts) or Etsy (printables) by hand

Rules it follows (Etsy's creativity standards, Aug 2026):
- Designs are original. It works from a niche's keywords, never from another
  shop's listing, photo or design.
- Every listing says it's printed by a production partner and, when artwork is
  AI-generated, says so.
- Brand, character, team and celebrity names get flagged for review.

No images are made yet: each t-shirt draft carries a design prompt. Turning
that into artwork needs an image tool Nathan picks (see docs).
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from datetime import datetime, timezone  # noqa: E402

from shared.config import ROOT  # noqa: E402
from shared.logger import DecisionLog, get_logger  # noqa: E402

from agents.common.approvals import ApprovalQueue  # noqa: E402
from agents.common.board import Board  # noqa: E402
from agents.common.bot import ip_flags, load_config, publishing_problem, report  # noqa: E402
from agents.common.brain import get_brain  # noqa: E402

AGENT = "studio"
STATE = ROOT / "state" / AGENT

# Etsy listing limits.
TITLE_MAX, TAGS_MAX, TAG_LEN_MAX = 140, 13, 20

# Words in a research keyword that name the product, not the subject.
SHIRT_WORDS = {"shirt", "shirts", "tshirt", "t-shirt", "tee", "tees", "sweatshirt", "hoodie"}
PRINTABLE_WORDS = {"printable", "planner", "checklist", "guide", "template", "pdf", "worksheet", "tracker"}
FILLER_WORDS = {"gift", "gifts", "for", "funny", "cute", "digital", "download"}

PRINTABLES = {
    "planner": ["Cover", "Monthly overview", "Weekly plan", "Daily page", "Goals tracker", "Notes"],
    "checklist": ["How to use this", "Main checklist", "Shopping list", "Budget sheet", "Notes"],
    "guide": ["Introduction", "Getting started", "Step-by-step", "Common mistakes", "Resources"],
}


def subject(niche: str) -> str:
    """'funny sourdough shirt' -> 'sourdough'. Falls back to the whole keyword."""
    words = [w for w in niche.lower().split() if w not in SHIRT_WORDS | PRINTABLE_WORDS | FILLER_WORDS]
    return " ".join(words) or niche


def product_types_for(cfg, niche: str) -> list[str]:
    """A keyword that names a product type only gets that type; a bare topic gets every type."""
    words = set(niche.lower().split())
    wanted = [t for t, vocab in (("tshirt", SHIRT_WORDS), ("printable", PRINTABLE_WORDS)) if words & vocab]
    return [t for t in cfg["product_types"] if t in (wanted or cfg["product_types"])]


def make_tags(niche: str, product: str, extra: list[str]) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9']+", niche.lower()) if len(w) > 2]
    candidates = [niche, *extra, f"{niche} {product}", *words, product, "gift idea"]
    tags, seen = [], set()
    for t in candidates:
        t = t.strip()
        if t and len(t) <= TAG_LEN_MAX and t not in seen:
            seen.add(t)
            tags.append(t)
    return tags[:TAGS_MAX]


def disclosure(cfg, ai_art: bool) -> str:
    lines = [f"Printed and shipped by our production partner, {cfg['production_partner']}."]
    if ai_art:
        lines.append("Artwork created with the help of AI tools and finished by us.")
    return "\n".join(lines)


def tshirt_drafts(cfg, niche: dict, brain) -> list[dict]:
    out = []
    for angle in cfg["design_angles"][:cfg["designs_per_niche"]]:
        copy = brain.listing_copy(subject(niche["niche"]), "t-shirt", angle)
        out.append({
            "product_type": "tshirt",
            "design_key": f"tshirt:{subject(niche['niche'])}:{angle}",
            "design_prompt": brain.design_prompt(subject(niche["niche"]), angle),
            "design_text": "[EDIT: the words on the shirt]",
            "listing": {
                "title": copy["title"][:TITLE_MAX],
                "tags": make_tags(niche["niche"], "shirt", [f"{angle} shirt"]),
                "description": copy["description"] + "\n\n" + disclosure(cfg, ai_art=True),
                "price_usd": suggested_price(cfg, niche, "tshirt"),
                "who_made": "i_did", "is_supply": False, "production_partner": cfg["production_partner"],
            },
        })
    return out


def printable_drafts(cfg, niche: dict, brain) -> list[dict]:
    out = []
    named = [w for w in niche["niche"].lower().split() if w in PRINTABLES]
    for kind in named or cfg["printable_types"]:
        copy = brain.listing_copy(subject(niche["niche"]), kind, "printable")
        out.append({
            "product_type": "printable",
            "design_key": f"printable:{subject(niche['niche'])}:{kind}",
            "document": {"format": "PDF, US Letter + A4", "pages": PRINTABLES[kind],
                         "content": "[EDIT: page content; the template brain only gives the outline]"},
            "listing": {
                "title": copy["title"][:TITLE_MAX],
                "tags": make_tags(niche["niche"], kind, [f"printable {kind}", "instant download"]),
                "description": copy["description"] + "\n\nInstant digital download. No physical item ships.",
                "price_usd": suggested_price(cfg, niche, "printable"),
                "who_made": "i_did", "is_digital": True,
            },
        })
    return out


def suggested_price(cfg, niche: dict, product_type: str) -> float:
    floor = cfg["price_floor_usd"][product_type]
    return round(max(floor, niche.get("avg_price") or floor), 2)


def draft(cfg, board: Board, queue: ApprovalQueue, decisions: DecisionLog, brain) -> list[dict]:
    niches = board.unread("niches", AGENT)
    # 'pickleball shirt' and 'pickleball gift' would otherwise yield the same shirts twice.
    made = {i["draft"].get("design_key") for i in queue.list(agent=AGENT)}
    submitted = []
    for niche in niches:
        types = product_types_for(cfg, niche["niche"])
        products = []
        if "tshirt" in types:
            products += tshirt_drafts(cfg, niche, brain)
        if "printable" in types:
            products += printable_drafts(cfg, niche, brain)
        for p in products:
            if p["design_key"] in made:
                decisions.record("skip_product", "same subject, type and angle already drafted", design_key=p["design_key"])
                continue
            made.add(p["design_key"])
            flags = ip_flags(niche["niche"], p["listing"]["title"], " ".join(p["listing"]["tags"]),
                             p.get("design_prompt", ""))
            item = queue.submit(AGENT, "etsy_listing", p["listing"]["title"], p, flags=flags, source_ids=[niche["id"]])
            decisions.record("draft_product", f"{p['product_type']} for niche '{niche['niche']}'",
                             approval_id=item["id"], flags=flags)
            submitted.append(item)
    board.ack("niches", AGENT, len(niches))
    return submitted


def listing_sheet(item: dict) -> str:
    d, lst = item["draft"], item["draft"]["listing"]
    where = ("Printify: create a t-shirt with your design, paste these fields, then Publish to Etsy"
             if d["product_type"] == "tshirt" else "Etsy: Add a listing > Digital, upload the PDF, paste these fields")
    lines = [f"## {lst['title']}", "", f"*{item['id']}* · {where}", ""]
    if d.get("design_prompt"):
        lines += ["**Design prompt** (for Canva or any image tool):", "", f"> {d['design_prompt']}", ""]
    if d.get("document"):
        lines += ["**Pages:** " + ", ".join(d["document"]["pages"]), ""]
    lines += ["**Title**", "", lst["title"], "", "**Tags** (13 max)", "", ", ".join(lst["tags"]), "",
              "**Price**", "", f"${lst['price_usd']:.2f}", "", "**Description**", "", lst["description"], ""]
    if item["flags"]:
        lines += ["**Flags:** " + "; ".join(item["flags"]), ""]
    return "\n".join(lines)


def export(queue: ApprovalQueue, decisions: DecisionLog, state_dir: Path = STATE, now: datetime | None = None) -> Path | None:
    """One Markdown sheet of every approved listing not exported before. Etsy charges $0.20 per listing,
    so nothing is uploaded automatically: Nathan pastes each one in, which is also the final check."""
    now = now or datetime.now(timezone.utc)
    state_dir.mkdir(parents=True, exist_ok=True)
    done_path = state_dir / "exported.json"
    done = set(json.loads(done_path.read_text())) if done_path.exists() else set()
    items = [i for i in queue.list(status="approved", kind="etsy_listing") if i["id"] not in done]
    if not items:
        return None
    out_dir = state_dir / "etsy_sheets"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{now:%Y-%m-%d-%H%M}.md"
    path.write_text(f"# Etsy listings to add ({len(items)})\n\nEach listing costs $0.20 on Etsy.\n\n"
                    + "\n---\n\n".join(listing_sheet(i) for i in items))
    for i in items:
        decisions.record("export_listing", "approved listing written to sheet", approval_id=i["id"], sheet=str(path))
    done_path.write_text(json.dumps(sorted(done | {i["id"] for i in items})))
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Terrarium studio agent")
    ap.add_argument("--task", choices=["check", "draft", "export"])
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
        logger.info(f"{len(items)} product drafts waiting for approval")
        if args.report:
            report(AGENT, logger, "idle", f"drafted {len(items)} products", len(items))
        return 0
    if args.task == "export":
        path = export(ApprovalQueue(), DecisionLog(AGENT))
        print(path or "no newly approved listings")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
