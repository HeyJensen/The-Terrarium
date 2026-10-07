"""Plumbing every commerce bot shares: config, the draft-only lock, inbox
files, the IP guard, and dashboard status.

Config comes from the agent's agent.json defaults, overridden by an optional
section with the agent's name in config/settings.json.
"""
import csv
import json
import re
from pathlib import Path

from shared.config import ROOT, load_settings
from shared.dashboard_client import build_status, post_status

INBOX_DIR = ROOT / "state" / "inbox"
IP_BLOCKLIST = Path(__file__).with_name("ip_blocklist.json")

# The only mode that exists. Publishing code is not written; adding a mode
# here is a decision for Nathan, not for an agent.
PUBLISHING_MODES = ("draft_only",)


def load_config(agent: str, settings: dict | None = None) -> dict:
    schema = json.loads((ROOT / "agents" / agent / "agent.json").read_text())["config_schema"]
    cfg = {k: v.get("default") for k, v in schema.items()}
    cfg.update((settings if settings is not None else load_settings()).get(agent, {}))
    return cfg


def publishing_problem(cfg: dict) -> str | None:
    mode = cfg.get("publishing_mode")
    if mode not in PUBLISHING_MODES:
        return f"publishing_mode {mode!r} is not allowed; only {list(PUBLISHING_MODES)} exists"
    return None


def read_inbox(agent: str, inbox_dir: Path | None = None) -> list[dict]:
    """Rows from every .csv / .json file Nathan drops in state/inbox/<agent>/.

    Headers are normalised to snake_case so exports from different tools line up.
    Files are moved to processed/ after reading so rows are never counted twice.
    """
    d = Path(inbox_dir) if inbox_dir else INBOX_DIR / agent
    d.mkdir(parents=True, exist_ok=True)
    rows = []
    for p in sorted(d.iterdir()):
        if p.suffix == ".csv":
            with open(p, newline="", encoding="utf-8-sig") as f:
                rows += [{_snake(k): v for k, v in r.items() if k} for r in csv.DictReader(f)]
        elif p.suffix == ".json":
            data = json.loads(p.read_text())
            rows += [{_snake(k): v for k, v in r.items()} for r in (data if isinstance(data, list) else [data])]
        else:
            continue
        done = d / "processed"
        done.mkdir(exist_ok=True)
        p.rename(done / p.name)
    return rows


def _snake(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.strip().lower()).strip("_")


def num(v, default: float = 0.0) -> float:
    """Parse '1,234', '$12.50', '45%' and blanks from tool exports."""
    if v is None:
        return default
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(v)) or default)
    except ValueError:
        return default


def ip_flags(*texts: str) -> list[str]:
    """Brand, character, team and celebrity names a design must not use.

    A hit doesn't block the draft; it puts a flag on it so the approval queue
    shows exactly why it needs a careful look.
    """
    terms = json.loads(IP_BLOCKLIST.read_text())["terms"]
    blob = " ".join(texts).lower()
    return [f"possible trademark: {t}" for t in terms if re.search(rf"\b{re.escape(t)}\b", blob)]


def report(agent: str, logger, status: str, task: str, tasks_today: int) -> None:
    # Commerce bots earn nothing until something is published, so pnl stays 0.
    post_status(build_status(agent, status, task, tasks_today, 0.0), logger)
