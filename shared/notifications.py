"""Trade alerts.

Phase 2: alerts go to the private Telegram channel. Sending is OFF until the
human approves it (settings notifications.telegram_enabled + env token),
because it posts outside the project. Until then alerts are only formatted
and written to the log.
"""
import json
import urllib.request

from shared.config import env


def format_trade_alert(side: str, symbol: str, price: float, rsi: float | None = None,
                       rel_vol: float | None = None, target_pct: float | None = None,
                       stop_pct: float | None = None, pnl: float | None = None,
                       reason: str | None = None) -> str:
    icon = {"BUY": "🟢", "SHORT": "🔴", "SELL": "⚪", "COVER": "⚪"}.get(side, "•")
    parts = [f"{icon} {side} {symbol} @ ${price:,.2f}"]
    if rsi is not None:
        parts.append(f"RSI(1m) {rsi:.1f}")
    if rel_vol is not None:
        parts.append(f"Vol {rel_vol:.1f}x avg")
    if target_pct is not None and stop_pct is not None:
        parts.append(f"Target +{target_pct:g}% / Stop -{stop_pct:g}%")
    if reason:
        parts.append(reason)
    if pnl is not None:
        parts.append(f"P&L {'+' if pnl >= 0 else '-'}${abs(pnl):,.2f}")
    return " | ".join(parts)


class Notifier:
    def __init__(self, settings: dict, logger):
        self.enabled = bool(settings.get("notifications", {}).get("telegram_enabled"))
        self.logger = logger

    def trade_alert(self, text: str) -> None:
        self.logger.info(f"ALERT {text}")
        if not self.enabled:
            return
        token, chat_id = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            self.logger.warning("Telegram enabled but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; alert not sent")
            return
        body = json.dumps({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as e:  # never let an alert failure stop trading logic
            self.logger.warning(f"Telegram send failed: {type(e).__name__}")
