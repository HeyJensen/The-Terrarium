"""Synthetic replay data for tests and demos. Deterministic, no network."""
import csv
from datetime import date, datetime, timedelta
from pathlib import Path

from shared.market_calendar import ET

SESSION_MINUTES = 390


def write_symbol(root: Path, symbol: str, days: dict[date, list[tuple[float, float, float, float, float]]],
                 prior_volume: float = 1_000_000, prior_days: int = 20) -> None:
    """days: {date: [(open, high, low, close, volume) per minute from 9:30]}"""
    (root / "minute").mkdir(parents=True, exist_ok=True)
    (root / "daily").mkdir(parents=True, exist_ok=True)
    with open(root / "minute" / f"{symbol}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for d, bars in sorted(days.items()):
            start = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
            for i, (o, h, l, c, v) in enumerate(bars):
                w.writerow([(start + timedelta(minutes=i)).isoformat(), o, h, l, c, v])
    first = min(days)
    with open(root / "daily" / f"{symbol}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "volume"])
        d = first - timedelta(days=prior_days * 2)
        rows = []
        while len(rows) < prior_days:
            d += timedelta(days=1)
            if d.weekday() < 5 and d < first:
                rows.append((d, prior_volume))
        for d, v in rows[-prior_days:]:
            w.writerow([d.isoformat(), v])
        # Completed session days in the replay also count as prior days for later days.
        for d, bars in sorted(days.items()):
            w.writerow([d.isoformat(), sum(b[4] for b in bars)])


def path_bars(closes: list[float], volume: float) -> list[tuple]:
    """Bars whose open is the previous close and whose high/low bracket open and close."""
    bars, prev = [], closes[0]
    for c in closes:
        bars.append((prev, max(prev, c), min(prev, c), c, volume))
        prev = c
    return bars


def flat(price: float, n: int, volume: float) -> list[float]:
    return [price] * n


def selloff_then(start: float, drop_minutes: int, step: float, then: list[float]) -> list[float]:
    closes = [start - step * i for i in range(drop_minutes)]
    return closes + then


def pad(closes: list[float], n: int = SESSION_MINUTES) -> list[float]:
    return closes + [closes[-1]] * (n - len(closes))
