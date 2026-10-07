"""The approval queue: nothing a commerce bot makes leaves the machine
until Nathan approves it.

Every draft (an Etsy listing, a blog post, a social post) is one JSON file in
state/approvals/<id>.json with status pending -> approved | rejected.
There is no publisher yet; when one is added it may only read approved items.

  python -m agents.common.approvals list [--status pending] [--kind blog_post]
  python -m agents.common.approvals show <id>
  python -m agents.common.approvals approve <id> [--note "..."]
  python -m agents.common.approvals reject <id> [--note "..."]
"""
import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.config import ROOT  # noqa: E402

QUEUE_DIR = ROOT / "state" / "approvals"
STATUSES = ("pending", "approved", "rejected")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ApprovalQueue:
    def __init__(self, queue_dir: Path = QUEUE_DIR):
        self.dir = Path(queue_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def submit(self, agent: str, kind: str, title: str, draft: dict, flags: list[str] | None = None,
               source_ids: list[str] | None = None) -> dict:
        """flags are reasons a human must look closely (e.g. a possible trademark)."""
        item = {
            "id": f"{kind}-{uuid.uuid4().hex[:8]}",
            "agent": agent,
            "kind": kind,
            "title": title,
            "status": "pending",
            "flags": flags or [],
            "source_ids": source_ids or [],
            "created_at": _now(),
            "decided_at": None,
            "note": None,
            "draft": draft,
        }
        self._write(item)
        return item

    def get(self, item_id: str) -> dict:
        p = self.dir / f"{item_id}.json"
        if not p.exists():
            raise KeyError(item_id)
        return json.loads(p.read_text())

    def list(self, status: str | None = None, kind: str | None = None, agent: str | None = None) -> list[dict]:
        items = [json.loads(p.read_text()) for p in sorted(self.dir.glob("*.json"))]
        items = [i for i in items if (status is None or i["status"] == status)
                 and (kind is None or i["kind"] == kind) and (agent is None or i["agent"] == agent)]
        return sorted(items, key=lambda i: i["created_at"])

    def decide(self, item_id: str, status: str, note: str | None = None) -> dict:
        if status not in ("approved", "rejected"):
            raise ValueError("status must be approved or rejected")
        item = self.get(item_id)
        item.update(status=status, decided_at=_now(), note=note)
        self._write(item)
        return item

    def _write(self, item: dict) -> None:
        (self.dir / f"{item['id']}.json").write_text(json.dumps(item, indent=2))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Terrarium approval queue")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--status", choices=STATUSES)
    ls.add_argument("--kind")
    sub.add_parser("show").add_argument("id")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.add_argument("--note")
    args = ap.parse_args(argv)
    q = ApprovalQueue()
    if args.cmd == "list":
        for i in q.list(args.status, args.kind):
            flags = f"  FLAGS: {', '.join(i['flags'])}" if i["flags"] else ""
            print(f"{i['id']:<28} {i['status']:<9} {i['agent']:<8} {i['title'][:70]}{flags}")
        return 0
    if args.cmd == "show":
        print(json.dumps(q.get(args.id), indent=2))
        return 0
    item = q.decide(args.id, "approved" if args.cmd == "approve" else "rejected", args.note)
    print(f"{item['id']} -> {item['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
