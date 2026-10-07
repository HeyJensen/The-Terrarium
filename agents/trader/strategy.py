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


def trend_allows(side: str, ema_fast: float | None, ema_slow: float | None) -> bool:
    """Daily trend filter: shorts only when the fast EMA is below the slow EMA
    (downtrend), longs only when it is above (uptrend). Unknown = not allowed."""
    if ema_fast is None or ema_slow is None:
        return False
    return ema_fast < ema_slow if side == SHORT else ema_fast > ema_slow


AFTER_TRIGGER_MODES = ("trail", "breakeven", "take_profit")


def initial_levels(entry: float, side: str, profit_trigger_pct: float, stop_loss_pct: float) -> tuple[float, float]:
    """(profit trigger, initial stop) measured from the entry fill price."""
    tp, sl = profit_trigger_pct / 100.0, stop_loss_pct / 100.0
    if side == LONG:
        return entry * (1 + tp), entry * (1 - sl)
    return entry * (1 - tp), entry * (1 + sl)


def bar_exit(side: str, bar_open: float, bar_high: float, bar_low: float,
             stop: float, target: float | None = None) -> tuple[str, float] | None:
    """Did this bar touch the stop (or the fixed target, if one is set)?
    Returns (reason, reference price).

    If the bar opened beyond a level (a gap), the reference is the open.
    If both levels are inside one bar we can't know the order, so we
    assume the stop hit first (conservative).
    """
    if side == LONG:
        if bar_open <= stop:
            return "stop_loss_gap", bar_open
        if target is not None and bar_open >= target:
            return "take_profit_gap", bar_open
        if bar_low <= stop:
            return "stop_loss", stop
        if target is not None and bar_high >= target:
            return "take_profit", target
    else:
        if bar_open >= stop:
            return "stop_loss_gap", bar_open
        if target is not None and bar_open <= target:
            return "take_profit_gap", bar_open
        if bar_high >= stop:
            return "stop_loss", stop
        if target is not None and bar_low <= target:
            return "take_profit", target
    return None


def ratchet_stop(side: str, entry: float, best: float, stop: float, trigger: float,
                 mode: str, trail_pct: float) -> float:
    """Move the stop in the position's favour once price has reached the +2%
    trigger. Never moves it backwards.

    trail      stop follows `trail_pct` behind the best price seen (at the
               trigger that locks in about +1%)
    breakeven  stop moves to the entry price and stays there
    """
    if side == LONG:
        if best < trigger:
            return stop
        new = best * (1 - trail_pct / 100.0) if mode == "trail" else entry
        return max(stop, new)
    if best > trigger:
        return stop
    new = best * (1 + trail_pct / 100.0) if mode == "trail" else entry
    return min(stop, new)


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
