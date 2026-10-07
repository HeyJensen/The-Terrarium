"""Structured logging for every agent.

- get_logger(name): JSON-lines log in logs/<name>.log plus readable stderr.
  A filter scrubs any secret env var value before a record is written.
- DecisionLog: append-only JSONL of every agent decision (the audit trail
  the human reviews before approving scale-up).
"""
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from shared.config import ROOT, secret_values

LOG_DIR = ROOT / "logs"
REDACTED = "[REDACTED]"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact(text: str) -> str:
    for value in secret_values():
        text = text.replace(value, REDACTED)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry)


def get_logger(name: str, log_dir: Path = LOG_DIR) -> logging.Logger:
    logger = logging.getLogger(f"organism.{name}")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(log_dir / f"{name}.log")
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(RedactingFilter())
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    console.addFilter(RedactingFilter())
    logger.addHandler(console)
    return logger


class DecisionLog:
    """One JSON object per line: what the agent decided, when, and why."""

    def __init__(self, agent: str, log_dir: Path = LOG_DIR):
        self.path = log_dir / agent / "decisions.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, action: str, reasoning: str, at: datetime | None = None, **fields) -> dict:
        entry = {
            "ts": (at or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            "logged_at": utcnow_iso(),
            "action": action,
            "reasoning": reasoning,
            **fields,
        }
        line = redact(json.dumps(entry, default=str))
        with open(self.path, "a") as f:
            f.write(line + "\n")
        return entry

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]
