"""Every-signal ledger for the website.

Each strategy signal (RSI + trend + fresh data) is scored as if it were taken
at its signal price with $1,000, with no one-trade-at-a-time limit, using the
same exit rules as the bot: -1% stop; once up +2% the stop trails 1% behind
the best price; closed at market after max_hold_days. Bots that take one
trade at a time compare their own fills against it.

Shape matches website/README.md ("Signal ledger").
"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from shared.market_calendar import to_et, trading_days_between

from agents.trader import strategy
from agents.trader.strategy import LONG

MAX_KEPT = 500
REASONS = {"stop_loss": "stop loss", "stop_loss_gap": "stop loss", "take_profit": "take profit",
           "take_profit_gap": "take profit"}


class SignalLedger:
    def __init__(self, path: Path, cfg: dict, agent: str = "trader"):
        self.path = Path(path)
        self.cfg = cfg
        self.agent = agent
        self.entries: list[dict] = json.loads(self.path.read_text()) if self.path.exists() else []
        # Internal tracking (stop, trigger, last bar checked) kept beside each public entry.
        self._track: dict[str, dict] = {e["id"]: e.pop("_track") for e in self.entries if "_track" in e}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{**e, "_track": self._track.get(e["id"])} for e in self.entries]
        self.path.write_text(json.dumps(rows))

    def open_symbols(self) -> list[str]:
        return [e["symbol"] for e in self.entries if e["status"] == "open"]

    def add(self, now: datetime, symbol: str, side: str, price: float, rsi: float | None, rel_vol: float) -> dict:
        sid = f"s{int(now.timestamp())}{symbol}"
        trigger, stop = strategy.initial_levels(price, side, float(self.cfg.get("profit_trigger_pct", 2.0)),
                                                float(self.cfg.get("stop_loss_pct", 1.0)))
        entry = {"id": sid, "agent": self.agent, "ts": now.isoformat(), "symbol": symbol,
                 "side": "buy" if side == LONG else "short", "price": round(price, 4), "rsi": rsi,
                 "rel_volume": rel_vol, "status": "open", "last_price": round(price, 4), "best_pct": 0.0,
                 "pnl_pct": 0.0, "exit_price": None, "exit_ts": None, "exit_reason": None}
        self._track[sid] = {"side": side, "stop": stop, "trigger": trigger, "best": price,
                            "day": to_et(now).date().isoformat(), "next_bar": now.isoformat()}
        self.entries.insert(0, entry)
        for old in self.entries[MAX_KEPT:]:
            self._track.pop(old["id"], None)
        del self.entries[MAX_KEPT:]
        return entry

    def update(self, feed, now: datetime) -> None:
        mode = self.cfg.get("after_profit_trigger", "trail")
        trail = float(self.cfg.get("trail_pct", 1.0))
        max_hold = int(self.cfg.get("max_hold_days", 5))
        today = to_et(now).date()
        for e in self.entries:
            if e["status"] != "open":
                continue
            t = self._track[e["id"]]
            start = datetime.fromisoformat(t["next_bar"])
            bars = [b for b in feed.minute_bars(e["symbol"], now) if b.ts >= start]
            target = t["trigger"] if mode == "take_profit" else None
            for bar in bars:
                t["next_bar"] = (bar.ts + timedelta(minutes=1)).isoformat()
                hit = strategy.bar_exit(t["side"], bar.open, bar.high, bar.low, t["stop"], target)
                if hit:
                    moved = t.get("stop_moved", False)
                    reason = "trailing stop" if hit[0].startswith("stop") and moved else REASONS[hit[0]]
                    self._close(e, t, hit[1], now, reason)
                    break
                t["best"] = max(t["best"], bar.high) if t["side"] == LONG else min(t["best"], bar.low)
                new = strategy.ratchet_stop(t["side"], e["price"], t["best"], t["stop"], t["trigger"], mode, trail)
                if new != t["stop"]:
                    t["stop"], t["stop_moved"] = new, True
                e["last_price"] = bar.close
            if e["status"] == "open":
                if trading_days_between(date.fromisoformat(t["day"]), today) >= max_hold and bars:
                    self._close(e, t, bars[-1].close, now, "max hold")
                else:
                    self._mark(e, t)

    def _pct(self, e: dict, price: float) -> float:
        direction = 1 if e["side"] == "buy" else -1
        return round(direction * (price / e["price"] - 1) * 100, 3)

    def _mark(self, e: dict, t: dict) -> None:
        e["pnl_pct"] = self._pct(e, e["last_price"])
        e["best_pct"] = max(e["best_pct"], self._pct(e, t["best"]))

    def _close(self, e: dict, t: dict, price: float, now: datetime, reason: str) -> None:
        e.update(status="closed", last_price=round(price, 4), exit_price=round(price, 4),
                 exit_ts=now.isoformat(), exit_reason=reason)
        e["pnl_pct"] = self._pct(e, price)
        e["best_pct"] = max(e["best_pct"], self._pct(e, t["best"]))

    def recent(self, n: int = 200) -> list[dict]:
        return self.entries[:n]
