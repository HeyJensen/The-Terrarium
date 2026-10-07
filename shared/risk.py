"""Hard-coded risk guardrails shared by every trading agent.

The constants below are ceilings. settings.json may be STRICTER, never
looser: the effective value is always min(config, ceiling). Changing a
ceiling is a code change the human must review.
"""
import json
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path

# Ceilings (non-negotiable)
MAX_DAILY_LOSS_PCT = 3.0          # kill switch at -3% of start-of-day equity
MAX_CONCURRENT_POSITIONS = 1      # one position at a time
MAX_RISK_PER_TRADE_PCT = 1.0      # 1% of account per trade
MAX_ACCOUNT_SIZE_USD = 1000.0     # sizing base; scaling up needs human approval + code review
MARGIN_MIN_EQUITY_USD = 2000.0    # FINRA Rule 4210 minimum equity for a margin account
VALID_MODES = ("cash", "margin")


class GuardrailViolation(Exception):
    pass


def effective_limits(cfg: dict) -> dict:
    """Clamp trader config to the hard ceilings; refuse invalid values."""
    mode = cfg.get("mode", "cash")
    if mode not in VALID_MODES:
        raise GuardrailViolation(f"mode must be one of {VALID_MODES}, got {mode!r}")
    if cfg.get("account_size_usd", 0) > MAX_ACCOUNT_SIZE_USD:
        raise GuardrailViolation(
            f"account_size_usd {cfg['account_size_usd']} exceeds the approved ${MAX_ACCOUNT_SIZE_USD:,.0f}. "
            "Scaling position size needs explicit human approval and a code change to MAX_ACCOUNT_SIZE_USD.")
    return {
        "mode": mode,
        "account_size_usd": float(cfg["account_size_usd"]),
        "daily_loss_limit_pct": min(float(cfg.get("daily_loss_limit_pct", MAX_DAILY_LOSS_PCT)), MAX_DAILY_LOSS_PCT),
        "max_concurrent_positions": min(int(cfg.get("max_concurrent_positions", 1)), MAX_CONCURRENT_POSITIONS),
        "risk_per_trade_pct": min(float(cfg.get("risk_per_trade_pct", MAX_RISK_PER_TRADE_PCT)), MAX_RISK_PER_TRADE_PCT),
    }


@dataclass
class DayState:
    day: str
    start_equity: float
    kill_switch: bool = False
    kill_reason: str = ""


class KillSwitch:
    """Daily loss kill switch, persisted so a restart can't reset it mid-day."""

    def __init__(self, limit_pct: float, state_path: Path):
        self.limit_pct = limit_pct
        self.state_path = state_path
        self.state: DayState | None = None
        if state_path.exists():
            try:
                self.state = DayState(**json.loads(state_path.read_text()))
            except (ValueError, TypeError):
                self.state = None

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(asdict(self.state)))

    def start_day(self, day: date, equity: float) -> None:
        if self.state is None or self.state.day != day.isoformat():
            self.state = DayState(day=day.isoformat(), start_equity=equity)
            self._save()

    @property
    def tripped(self) -> bool:
        return bool(self.state and self.state.kill_switch)

    def day_pnl_pct(self, equity: float) -> float:
        return (equity / self.state.start_equity - 1.0) * 100.0

    def check(self, equity: float) -> bool:
        """Returns True if the switch is (now) tripped."""
        if self.tripped:
            return True
        pct = self.day_pnl_pct(equity)
        if pct <= -self.limit_pct:
            self.state.kill_switch = True
            self.state.kill_reason = f"daily loss {pct:.2f}% hit limit -{self.limit_pct:g}%"
            self._save()
            return True
        return False
