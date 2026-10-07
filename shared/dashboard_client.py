"""Agents report status to the dashboard API with this client."""
import json
import urllib.request
from datetime import datetime, timezone

from shared.config import env

STATUS_FIELDS = ("name", "status", "current_task", "tasks_today", "pnl", "last_updated")


def build_status(name: str, status: str, current_task: str, tasks_today: int, pnl: float) -> dict:
    return {
        "name": name,
        "status": status,
        "current_task": current_task,
        "tasks_today": int(tasks_today),
        "pnl": round(float(pnl), 2),
        "last_updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def post_status(status: dict, logger=None, base_url: str | None = None) -> bool:
    base_url = base_url or env("DASHBOARD_URL") or "http://127.0.0.1:8787"
    req = urllib.request.Request(
        f"{base_url}/api/agents/{status['name']}/status",
        data=json.dumps(status).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {env('DASHBOARD_TOKEN') or ''}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5).close()
        return True
    except Exception as e:  # dashboard being down must never stop an agent
        if logger:
            logger.warning(f"dashboard status post failed: {type(e).__name__}")
        return False


def post_outbox(agent: str, title: str, detail: str, logger=None, base_url: str | None = None) -> bool:
    base_url = base_url or env("DASHBOARD_URL") or "http://127.0.0.1:8787"
    req = urllib.request.Request(
        f"{base_url}/api/outbox",
        data=json.dumps({"agent": agent, "title": title, "detail": detail}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {env('DASHBOARD_TOKEN') or ''}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5).close()
        return True
    except Exception as e:
        if logger:
            logger.warning(f"dashboard outbox post failed: {type(e).__name__}")
        return False


def post_signals(agent: str, signals: list[dict], logger=None, base_url: str | None = None) -> bool:
    base_url = base_url or env("DASHBOARD_URL") or "http://127.0.0.1:8787"
    req = urllib.request.Request(
        f"{base_url}/api/signals",
        data=json.dumps({"agent": agent, "signals": signals}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {env('DASHBOARD_TOKEN') or ''}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5).close()
        return True
    except Exception as e:
        if logger:
            logger.warning(f"dashboard signals post failed: {type(e).__name__}")
        return False
