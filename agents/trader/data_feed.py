"""Market data sources.

MarketData is the interface the engine needs: completed 1-minute bars for
today's session, and prior daily volumes for relative volume.

ReplayFeed reads CSVs so the full engine can be exercised without a broker:
  <dir>/minute/<SYMBOL>.csv   ts,open,high,low,close,volume   (ts ISO-8601 with offset, bar START time)
  <dir>/daily/<SYMBOL>.csv    date,volume,close   (close needed for the EMA trend filter)

A live 1-minute feed is NOT wired yet: Robinhood's agentic MCP does not
document historical intraday bars (see docs/robinhood-oauth-setup.md), so
the source is a decision for the human.
"""
import csv
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from shared.market_calendar import to_et


@dataclass(frozen=True)
class Bar:
    ts: datetime  # bar start, tz-aware
    open: float
    high: float
    low: float
    close: float
    volume: float


class MarketData(ABC):
    @abstractmethod
    def minute_bars(self, symbol: str, now: datetime) -> list[Bar]:
        """Today's regular-session 1-minute bars that have fully closed before `now`."""

    @abstractmethod
    def prior_daily_volumes(self, symbol: str, day: date, n: int) -> list[float]:
        """Full-day volumes for the n trading days before `day`, oldest first."""

    @abstractmethod
    def prior_daily_closes(self, symbol: str, day: date, n: int) -> list[float]:
        """Daily closing prices for the n trading days before `day`, oldest first."""


class ReplayFeed(MarketData):
    def __init__(self, root: Path):
        self.root = Path(root)
        self._minute: dict[str, list[Bar]] = {}
        self._daily: dict[str, list[tuple[date, float, float | None]]] = {}

    def symbols(self) -> list[str]:
        return sorted(p.stem for p in (self.root / "minute").glob("*.csv"))

    def _load_minute(self, symbol: str) -> list[Bar]:
        if symbol not in self._minute:
            path = self.root / "minute" / f"{symbol}.csv"
            bars = []
            if path.exists():
                with open(path) as f:
                    for row in csv.DictReader(f):
                        bars.append(Bar(datetime.fromisoformat(row["ts"]), float(row["open"]), float(row["high"]),
                                        float(row["low"]), float(row["close"]), float(row["volume"])))
            self._minute[symbol] = sorted(bars, key=lambda b: b.ts)
        return self._minute[symbol]

    def _load_daily(self, symbol: str) -> list[tuple[date, float, float | None]]:
        if symbol not in self._daily:
            path = self.root / "daily" / f"{symbol}.csv"
            rows = []
            if path.exists():
                with open(path) as f:
                    rows = [(date.fromisoformat(r["date"]), float(r["volume"]),
                             float(r["close"]) if r.get("close") else None) for r in csv.DictReader(f)]
            self._daily[symbol] = sorted(rows)
        return self._daily[symbol]

    def minute_bars(self, symbol: str, now: datetime) -> list[Bar]:
        today = to_et(now).date()
        return [b for b in self._load_minute(symbol)
                if to_et(b.ts).date() == today and b.ts + timedelta(minutes=1) <= now]

    def prior_daily_volumes(self, symbol: str, day: date, n: int) -> list[float]:
        prior = [v for d, v, _ in self._load_daily(symbol) if d < day]
        return prior[-n:]

    def prior_daily_closes(self, symbol: str, day: date, n: int) -> list[float]:
        prior = [c for d, _, c in self._load_daily(symbol) if d < day and c is not None]
        return prior[-n:]

    def timeline(self) -> list[datetime]:
        """Every distinct bar-close time in the data, for driving a replay clock."""
        times = set()
        for s in self.symbols():
            times.update(b.ts + timedelta(minutes=1) for b in self._load_minute(s))
        return sorted(times)
