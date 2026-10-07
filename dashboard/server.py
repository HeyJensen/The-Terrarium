"""Dashboard status API: the one place every agent reports to, and what the
Terrarium website reads.

Standard library only. Binds to 127.0.0.1 by default: nothing is public
until the human decides how to expose it.

  GET  /api/agents                     all agents from agents/manifest.json + latest status
  GET  /api/agents/<name>/status       one agent's status
  POST /api/agents/<name>/status       agent reports {name,status,current_task,tasks_today,pnl,last_updated}
  GET  /api/outbox                     finished-work feed, newest first
  POST /api/outbox                     {agent,title,detail}
  GET  /api/heartbeat                  activity series: [{minute, events}] for the last 24h
  GET  /api/signals                    every-signal ledger, newest first (shape: website/README.md)
  POST /api/signals                    {agent, signals: [...]} replaces that agent's ledger
  GET  /api/dashboard                  {agents, outbox, heartbeat, signals} in one call
  GET  /healthz

POSTs need "Authorization: Bearer $DASHBOARD_TOKEN" when DASHBOARD_TOKEN is set.

Run: python -m dashboard.server
"""
import hmac
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from shared.config import ROOT, env, load_settings
from shared.dashboard_client import STATUS_FIELDS
from shared.logger import get_logger

DATA_DIR = ROOT / "dashboard" / "data"
MANIFEST = ROOT / "agents" / "manifest.json"
OUTBOX_MAX = 200
SIGNALS_MAX = 500
HEARTBEAT_WINDOW = timedelta(hours=24)
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate_status(name: str, body: dict) -> dict:
    if set(body) != set(STATUS_FIELDS):
        missing, extra = set(STATUS_FIELDS) - set(body), set(body) - set(STATUS_FIELDS)
        raise ValueError(f"status must have exactly {list(STATUS_FIELDS)}; missing={sorted(missing)} extra={sorted(extra)}")
    if body["name"] != name:
        raise ValueError("body name does not match URL")
    if not isinstance(body["status"], str) or not isinstance(body["current_task"], str):
        raise ValueError("status and current_task must be strings")
    if not isinstance(body["tasks_today"], int) or isinstance(body["tasks_today"], bool):
        raise ValueError("tasks_today must be an integer")
    if not isinstance(body["pnl"], (int, float)) or isinstance(body["pnl"], bool):
        raise ValueError("pnl must be a number")
    datetime.fromisoformat(body["last_updated"])  # raises ValueError if malformed
    return {k: body[k] for k in STATUS_FIELDS}


class Store:
    def __init__(self, data_dir: Path = DATA_DIR, manifest: Path = MANIFEST):
        self.path = data_dir / "state.json"
        self.manifest = manifest
        self.lock = threading.Lock()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data = {"agents": {}, "outbox": [], "heartbeat": {}, "signals": {}}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text()))

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)

    def _beat(self) -> None:
        minute = _now().replace(second=0, microsecond=0)
        key = minute.isoformat()
        hb = self.data["heartbeat"]
        hb[key] = hb.get(key, 0) + 1
        cutoff = (minute - HEARTBEAT_WINDOW).isoformat()
        for k in [k for k in hb if k < cutoff]:
            del hb[k]

    def registered_agents(self) -> list[str]:
        if not self.manifest.exists():
            return []
        return [a["name"] for a in json.loads(self.manifest.read_text()).get("agents", [])]

    def agents(self) -> list[dict]:
        with self.lock:
            out = []
            for name in self.registered_agents():
                out.append(self.data["agents"].get(name) or {
                    "name": name, "status": "idle", "current_task": "", "tasks_today": 0,
                    "pnl": 0.0, "last_updated": None})
            return out

    def agent(self, name: str) -> dict | None:
        return next((a for a in self.agents() if a["name"] == name), None)

    def set_status(self, name: str, status: dict) -> None:
        with self.lock:
            self.data["agents"][name] = status
            self._beat()
            self._save()

    def add_outbox(self, item: dict) -> dict:
        with self.lock:
            entry = {"agent": item["agent"], "title": item["title"], "detail": item.get("detail", ""),
                     "ts": _now().isoformat(timespec="seconds")}
            self.data["outbox"].insert(0, entry)
            del self.data["outbox"][OUTBOX_MAX:]
            self._beat()
            self._save()
            return entry

    def outbox(self) -> list[dict]:
        with self.lock:
            return list(self.data["outbox"])

    def set_signals(self, agent: str, signals: list[dict]) -> None:
        with self.lock:
            self.data.setdefault("signals", {})[agent] = signals[:SIGNALS_MAX]
            self._save()

    def signals(self) -> list[dict]:
        with self.lock:
            rows = [s for sigs in self.data.get("signals", {}).values() for s in sigs]
        return sorted(rows, key=lambda s: s.get("ts", ""), reverse=True)

    def heartbeat(self) -> list[dict]:
        with self.lock:
            return [{"minute": k, "events": v} for k, v in sorted(self.data["heartbeat"].items())]


def make_handler(store: Store, logger):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            logger.info(f"{self.address_string()} {fmt % args}")

        def _send(self, code: int, payload) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")  # website reads cross-origin
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            token = env("DASHBOARD_TOKEN")
            if not token:
                return True
            given = self.headers.get("Authorization", "")
            return hmac.compare_digest(given, f"Bearer {token}")

        def _json_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if length > 64_000:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(length) or b"{}")

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.end_headers()

        def do_GET(self):
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            if parts == ["healthz"]:
                return self._send(200, {"ok": True})
            if parts == ["api", "agents"]:
                return self._send(200, store.agents())
            if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] == "status":
                agent = store.agent(parts[2])
                return self._send(200, agent) if agent else self._send(404, {"error": "unknown agent"})
            if parts == ["api", "outbox"]:
                return self._send(200, store.outbox())
            if parts == ["api", "heartbeat"]:
                return self._send(200, store.heartbeat())
            if parts == ["api", "signals"]:
                return self._send(200, store.signals())
            if parts == ["api", "dashboard"]:
                return self._send(200, {"agents": store.agents(), "outbox": store.outbox(),
                                        "heartbeat": store.heartbeat(), "signals": store.signals()})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            try:
                body = self._json_body()
                if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] == "status":
                    name = parts[2]
                    if not NAME_RE.match(name) or name not in store.registered_agents():
                        return self._send(404, {"error": "agent not in agents/manifest.json"})
                    status = validate_status(name, body)
                    store.set_status(name, status)
                    return self._send(200, status)
                if parts == ["api", "signals"]:
                    agent, sigs = body.get("agent"), body.get("signals")
                    if agent not in store.registered_agents() or not isinstance(sigs, list):
                        return self._send(400, {"error": "need a registered agent and a signals list"})
                    store.set_signals(agent, sigs)
                    return self._send(200, {"stored": min(len(sigs), SIGNALS_MAX)})
                if parts == ["api", "outbox"]:
                    if body.get("agent") not in store.registered_agents() or not body.get("title"):
                        return self._send(400, {"error": "need a registered agent and a title"})
                    return self._send(201, store.add_outbox(body))
            except (ValueError, KeyError) as e:
                return self._send(400, {"error": str(e)})
            return self._send(404, {"error": "not found"})

    return Handler


def serve(host: str | None = None, port: int | None = None, store: Store | None = None) -> ThreadingHTTPServer:
    settings = load_settings()
    host = host or settings["dashboard"]["host"]
    port = port if port is not None else settings["dashboard"]["port"]
    logger = get_logger("dashboard")
    if not env("DASHBOARD_TOKEN"):
        logger.warning("DASHBOARD_TOKEN not set: status POSTs are unauthenticated (fine on 127.0.0.1 only)")
    server = ThreadingHTTPServer((host, port), make_handler(store or Store(), logger))
    logger.info(f"dashboard API on http://{host}:{server.server_port}")
    return server


if __name__ == "__main__":
    serve().serve_forever()
