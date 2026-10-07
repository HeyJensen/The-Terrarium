"""The exact Phase 1 strategy spec, as pure functions (no I/O)."""
import math

LONG, SHORT = "long", "short"


def top_by_relative_volume(rel_vols: dict[str, float], top_n: int) -> list[tuple[str, float]]:
    ranked = sorted(rel_vols.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:top_n]


def entry_signal(rsi_value: float | None, short_at_or_above: float, long_at_or_below: float) -> str | None:
    if rsi_value is None:
        return None
    if rsi_value >= short_at_or_above:
        return SHORT
    if rsi_value <= long_at_or_below:
        return LONG
    return None


def exit_levels(entry: float, side: str, take_profit_pct: float, stop_loss_pct: float) -> tuple[float, float]:
    """(target, stop) measured from the entry fill price."""
    tp, sl = take_profit_pct / 100.0, stop_loss_pct / 100.0
    if side == LONG:
        return entry * (1 + tp), entry * (1 - sl)
    return entry * (1 - tp), entry * (1 + sl)


def bar_exit(side: str, bar_open: float, bar_high: float, bar_low: float,
             target: float, stop: float) -> tuple[str, float] | None:
    """Did this bar touch the stop or target? Returns (reason, reference price).

    If the bar opened beyond a level (a gap), the reference is the open.
    If both levels are inside one bar we can't know the order, so we
    assume the stop hit first (conservative).
    """
    if side == LONG:
        if bar_open <= stop:
            return "stop_loss_gap", bar_open
        if bar_open >= target:
            return "take_profit_gap", bar_open
        if bar_low <= stop:
            return "stop_loss", stop
        if bar_high >= target:
            return "take_profit", target
    else:
        if bar_open >= stop:
            return "stop_loss_gap", bar_open
        if bar_open <= target:
            return "take_profit_gap", bar_open
        if bar_high >= stop:
            return "stop_loss", stop
        if bar_low <= target:
            return "take_profit", target
    return None


def position_size(account_size: float, risk_pct: float, stop_pct: float, price: float,
                  available_cash: float, allow_fractional: bool) -> float:
    """Risk risk_pct of the account with a stop_pct stop, capped by available cash.

    $1,000 x 1% risk / 1% stop = $1,000 notional: the whole account, one position.
    """
    if price <= 0 or stop_pct <= 0:
        return 0.0
    notional = min(account_size * risk_pct / stop_pct, available_cash)
    qty = notional / price
    return math.floor(qty * 10_000) / 10_000 if allow_fractional else float(math.floor(qty))
