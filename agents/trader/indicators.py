"""Indicators used by the strategy spec."""


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI on the given closes. None until period+1 closes exist."""
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for prev, cur in zip(closes[:period], closes[1:period + 1]):
        change = cur - prev
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for prev, cur in zip(closes[period:-1], closes[period + 1:]):
        change = cur - prev
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def relative_volume(today_volume: float, prior_daily_volumes: list[float]) -> float | None:
    """Today's volume so far divided by the average of the prior N full days."""
    if not prior_daily_volumes:
        return None
    avg = sum(prior_daily_volumes) / len(prior_daily_volumes)
    return today_volume / avg if avg > 0 else None


def ema(values: list[float], period: int) -> float | None:
    """Exponential moving average of the series, seeded with the SMA of the
    first `period` values. None until `period` values exist."""
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    value = sum(values[:period]) / period
    for v in values[period:]:
        value = v * k + value * (1 - k)
    return value
