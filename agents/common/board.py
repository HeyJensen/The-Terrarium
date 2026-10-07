"""The idea board: how the commerce bots hand work to each other.

Each topic is an append-only JSONL file in state/board/<topic>.jsonl. A
producer posts items; each consumer keeps its own cursor, so one item can
feed several bots (a trend feeds Etsy research, the blog, and the studio).

Topics:
  trends     social  -> etsy, content, studio   what people are talking about
  niches     etsy    -> studio, content         product niches worth making
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from shared.config import ROOT

BOARD_DIR = ROOT / "state" / "board"


class Board:
    def __init__(self, board_dir: Path = BOARD_DIR):
        self.dir = Path(board_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, topic: str) -> Path:
        return self.dir / f"{topic}.jsonl"

    def post(self, topic: str, source: str, payload: dict) -> dict:
        item = {
            "id": uuid.uuid4().hex[:12],
            "topic": topic,
            "source": source,
            "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **payload,
        }
        with open(self._path(topic), "a") as f:
            f.write(json.dumps(item) + "\n")
        return item

    def read(self, topic: str) -> list[dict]:
        p = self._path(topic)
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]

    def unread(self, topic: str, consumer: str) -> list[dict]:
        """Items this consumer hasn't taken yet. Call ack() once they're handled."""
        return self.read(topic)[self._cursor(topic, consumer):]

    def ack(self, topic: str, consumer: str, count: int) -> None:
        cur = self._cursor_path(topic, consumer)
        cur.write_text(json.dumps({"offset": self._cursor(topic, consumer) + count}))

    def _cursor_path(self, topic: str, consumer: str) -> Path:
        return self.dir / f".cursor.{consumer}.{topic}.json"

    def _cursor(self, topic: str, consumer: str) -> int:
        p = self._cursor_path(topic, consumer)
        return json.loads(p.read_text())["offset"] if p.exists() else 0
