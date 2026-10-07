"""Builder agent: keeps the Terrarium healthy and proposes new money-making bots.

  python agents/builder/run.py --status
  python agents/builder/run.py --task check
  python agents/builder/run.py --task audit              house-rule check of every bot -> state/builder/audit.md
  python agents/builder/run.py --task ideas              best ideas -> approval queue as bot_idea items
  python agents/builder/run.py --task scaffold --idea <approval id>
                                                         approved idea -> agents/<name>/, draft-only, disabled

The builder never merges, publishes, buys or trades. A weekly Claude routine
runs it, adds fresh ideas to ideas.json, and opens a draft pull request for
Nathan; see prompt.md.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.config import ROOT  # noqa: E402
from shared.logger import DecisionLog, get_logger  # noqa: E402

from agents.common.approvals import ApprovalQueue  # noqa: E402
from agents.common.bot import load_config, publishing_problem, read_inbox, report  # noqa: E402

AGENT = "builder"
CATALOG = Path(__file__).with_name("ideas.json")
STATE_DIR = ROOT / "state" / AGENT
NOT_BOTS = {"common", "__pycache__"}
REQUIRED_FILES = ("agent.json", "prompt.md", "run.py", "__init__.py")

NEED_FLAGS = {
    "paid_service": "needs a paid service (Nathan approves spend)",
    "new_dependency": "needs a new Python package (Nathan approves)",
    "public_posting": "posts publicly (Nathan approves each publisher)",
    "account": "needs an account Nathan creates",
}


def score(idea: dict) -> int:
    return idea["potential"] + idea["speed"] + idea["reuse"] - idea["cost"] - idea["risk"]


def existing_bots(root: Path = ROOT) -> list[str]:
    return sorted(p.name for p in (root / "agents").iterdir() if p.is_dir() and p.name not in NOT_BOTS)


# --- audit -----------------------------------------------------------------

def audit(root: Path, queue: ApprovalQueue, cfg: dict, now: datetime | None = None) -> list[dict]:
    """Every finding is {severity, bot, finding}. high = breaks a house rule."""
    now = now or datetime.now(timezone.utc)
    findings = []
    manifest = {a["name"] for a in json.loads((root / "agents" / "manifest.json").read_text())["agents"]}
    tests = "\n".join(p.read_text() for p in (root / "tests").glob("test_*.py"))
    for bot in existing_bots(root):
        d = root / "agents" / bot
        for f in REQUIRED_FILES:
            if not (d / f).exists():
                findings.append({"severity": "high", "bot": bot, "finding": f"missing {f}"})
        if bot not in manifest:
            findings.append({"severity": "high", "bot": bot, "finding": "not in agents/manifest.json"})
        if not re.search(rf"\bagents\.{re.escape(bot)}\b", tests):
            findings.append({"severity": "medium", "bot": bot, "finding": "no test imports it"})
        for p in sorted(d.rglob("*.py")):
            for n, line in enumerate(p.read_text().splitlines(), 1):
                if re.search(r"#\s*(TODO|FIXME|XXX)\b", line):
                    findings.append({"severity": "low", "bot": bot,
                                     "finding": f"{p.relative_to(root)}:{n} {line.strip()[:80]}"})
    for name in manifest - set(existing_bots(root)):
        findings.append({"severity": "high", "bot": name, "finding": "in the manifest but has no folder"})
    cutoff = now - timedelta(days=cfg["stale_approval_days"])
    stale = [i for i in queue.list(status="pending") if datetime.fromisoformat(i["created_at"]) < cutoff]
    by_bot: dict[str, int] = {}
    for i in stale:
        by_bot[i["agent"]] = by_bot.get(i["agent"], 0) + 1
    for bot, n in sorted(by_bot.items()):
        findings.append({"severity": "medium", "bot": bot,
                         "finding": f"{n} drafts waiting over {cfg['stale_approval_days']} days for approval"})
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(findings, key=lambda f: (order[f["severity"]], f["bot"]))


def audit_markdown(findings: list[dict], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    lines = [f"# Builder audit, {now:%Y-%m-%d %H:%M} UTC", ""]
    if not findings:
        return "\n".join(lines + ["Every bot follows the house rules. Nothing to fix.", ""])
    for sev in ("high", "medium", "low"):
        rows = [f for f in findings if f["severity"] == sev]
        if rows:
            lines += [f"## {sev.title()} ({len(rows)})", ""] + [f"- **{f['bot']}**: {f['finding']}" for f in rows] + [""]
    return "\n".join(lines)


# --- ideas -----------------------------------------------------------------

def load_ideas(catalog: Path = CATALOG, inbox_rows: list[dict] | None = None) -> list[dict]:
    """Catalog ideas plus idea files dropped in state/inbox/builder/ (same fields)."""
    ideas = json.loads(Path(catalog).read_text())["ideas"]
    known = {i["id"] for i in ideas}
    for row in inbox_rows or []:
        try:
            idea = {**row, **{k: int(row[k]) for k in ("potential", "speed", "reuse", "cost", "risk")}}
        except (KeyError, ValueError):
            continue
        if idea.get("id") and idea["id"] not in known:
            idea.setdefault("needs", [])
            idea.setdefault("reuses", [])
            ideas.append(idea)
            known.add(idea["id"])
    return ideas


def propose_ideas(cfg: dict, ideas: list[dict], bots: list[str], queue: ApprovalQueue,
                  decisions: DecisionLog) -> list[dict]:
    proposed = {i["draft"]["id"] for i in queue.list(kind="bot_idea")}
    ranked = sorted(ideas, key=score, reverse=True)
    submitted = []
    for idea in ranked:
        if len(submitted) >= cfg["ideas_per_run"]:
            break
        why_not = (f"blocked: {idea['blocked']}" if idea.get("blocked")
                   else "already a bot" if idea["name"] in bots
                   else "already proposed" if idea["id"] in proposed
                   else f"score {score(idea)} under {cfg['min_idea_score']}" if score(idea) < cfg["min_idea_score"]
                   else None)
        if why_not:
            decisions.record("skip_idea", why_not, idea=idea["id"])
            continue
        draft = {**idea, "score": score(idea)}
        flags = [NEED_FLAGS.get(n, f"needs {n}") for n in idea.get("needs", [])]
        item = queue.submit(AGENT, "bot_idea", f"[{score(idea)}] {idea['title']}", draft, flags=flags)
        decisions.record("propose_idea", f"score {score(idea)}", idea=idea["id"], approval_id=item["id"])
        submitted.append(item)
    return submitted


# --- scaffold --------------------------------------------------------------

def scaffold(item: dict, root: Path = ROOT) -> Path:
    """Create agents/<name>/ for an approved bot_idea and register it, disabled."""
    if item["kind"] != "bot_idea":
        raise ValueError(f"{item['id']} is a {item['kind']}, not a bot_idea")
    if item["status"] != "approved":
        raise ValueError(f"{item['id']} is {item['status']}; approve it first")
    idea = item["draft"]
    name = idea["name"]
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", name):
        raise ValueError(f"bad bot name {name!r}")
    d = root / "agents" / name
    if d.exists():
        raise FileExistsError(f"agents/{name} already exists")
    d.mkdir(parents=True)
    (d / "__init__.py").write_text("")
    (d / "agent.json").write_text(json.dumps({
        "name": name,
        "type": idea.get("stream", "new"),
        "description": idea["title"],
        "version": "0.0.1",
        "entrypoint": f"agents/{name}/run.py",
        "stream": idea.get("stream", name),
        "config_schema": {
            "publishing_mode": {"type": "string", "enum": ["draft_only"], "default": "draft_only"},
        },
    }, indent=2) + "\n")
    needs = "\n".join(f"- {NEED_FLAGS.get(n, n)}" for n in idea.get("needs", [])) or "- nothing beyond the repo"
    (d / "prompt.md").write_text(
        f"# Agent: {name.title()}\n\n## Job\n{idea['pitch']}\n\n"
        f"Plugs into: {', '.join(idea.get('reuses', [])) or 'nothing yet'}.\n\n"
        f"## Before it can go live\n{needs}\n\n"
        "## Hard rules\n- Draft-only. Everything it makes waits in the approval queue.\n"
        "- No spending, posting or new dependencies without Nathan's OK.\n"
        "- Secrets only in environment variables.\n\n"
        f"Scaffolded by the builder from approved idea {item['id']}. [EDIT: real job description]\n")
    (d / "run.py").write_text(RUN_TEMPLATE.format(name=name, title=idea["title"]))
    manifest_path = root / "agents" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["agents"].append({"name": name, "path": f"agents/{name}", "type": idea.get("stream", "new"),
                               "stream": idea.get("stream", name), "enabled": False})
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return d


RUN_TEMPLATE = '''"""{title}

Scaffolded by the builder agent. Draft-only until Nathan approves a publisher.

  python agents/{name}/run.py --status
  python agents/{name}/run.py --task check
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agents.common.approvals import ApprovalQueue  # noqa: E402
from agents.common.bot import load_config, publishing_problem  # noqa: E402

AGENT = "{name}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Terrarium {name} agent")
    ap.add_argument("--task", choices=["check"])
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(AGENT)
    problem = publishing_problem(cfg)
    if args.status:
        print(json.dumps({{"name": AGENT, "pending": len(ApprovalQueue().list(status="pending", agent=AGENT))}}))
        return 0
    if args.task == "check":
        print(json.dumps({{"config": cfg, "publishing_problem": problem}}, indent=2))
        return 0 if problem is None else 1
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Terrarium builder agent")
    ap.add_argument("--task", choices=["check", "audit", "ideas", "scaffold"])
    ap.add_argument("--idea", help="approval id of an approved bot_idea (for --task scaffold)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--report", action="store_true", help="post status to the dashboard API")
    args = ap.parse_args(argv)
    cfg = load_config(AGENT)
    logger = get_logger(AGENT)
    problem = publishing_problem(cfg)
    queue = ApprovalQueue()
    if args.status:
        print(json.dumps({"name": AGENT, "bots": existing_bots(),
                          "ideas_pending": len(queue.list(status="pending", kind="bot_idea")),
                          "ideas_approved": len(queue.list(status="approved", kind="bot_idea"))}, indent=2))
        return 0
    if args.task == "check":
        print(json.dumps({"config": cfg, "publishing_problem": problem}, indent=2))
        return 0 if problem is None else 1
    if problem:
        logger.error(f"refusing to run: {problem}")
        return 2
    if args.task == "audit":
        findings = audit(ROOT, queue, cfg)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "audit.json").write_text(json.dumps(findings, indent=2))
        md = audit_markdown(findings)
        (STATE_DIR / "audit.md").write_text(md)
        print(md)
        if args.report:
            report(AGENT, logger, "idle", f"audit: {len(findings)} findings", 1)
        return 0
    if args.task == "ideas":
        ideas = load_ideas(inbox_rows=read_inbox(AGENT))
        items = propose_ideas(cfg, ideas, existing_bots(), queue, DecisionLog(AGENT))
        for i in items:
            print(f"{i['id']:<22} {i['title']}")
        logger.info(f"{len(items)} bot ideas waiting for approval")
        if args.report:
            report(AGENT, logger, "idle", f"proposed {len(items)} bot ideas", len(items))
        return 0
    if args.task == "scaffold":
        if not args.idea:
            ap.error("--task scaffold needs --idea <approval id>")
        d = scaffold(queue.get(args.idea))
        DecisionLog(AGENT).record("scaffold", "approved idea", approval_id=args.idea, path=str(d.relative_to(ROOT)))
        print(f"created {d.relative_to(ROOT)} (disabled in the manifest until you enable it)")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
