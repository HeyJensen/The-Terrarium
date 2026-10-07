"""Live terminal view: what the Trader sees each minute and which
requirements each candidate meets."""
from datetime import datetime

from shared.market_calendar import is_regular_hours, to_et

OK, NO, NA = "✓", "✗", "·"
COLS = [("rsi", "RSI"), ("trend", "Trend"), ("fresh_data", "Fresh"), ("side_allowed", "Side"), ("no_open_trade", "Flat")]


def _mark(row: dict, key: str) -> str:
    if key in ("trend", "side_allowed") and not row["signal"]:
        return NA
    return OK if row["checks"][key] else NO


def render(engine, now: datetime, routing: str) -> str:
    cfg = engine.cfg
    et = to_et(now)
    lines = [
        "THE TERRARIUM · Trader",
        f"{et:%a %b %d %H:%M:%S} ET · market {'OPEN' if is_regular_hours(now) else 'CLOSED'} · "
        f"{'DRY RUN (no real orders)' if routing == 'dry_run' else 'LIVE ORDERS'} · mode {engine.limits['mode']}",
        f"Rules: buy RSI(1m) ≤ {cfg.get('rsi_long_at_or_below', 15)} in an uptrend · short RSI ≥ "
        f"{cfg.get('rsi_short_at_or_above', 85)} in a downtrend (50/200 EMA, daily) · top "
        f"{cfg.get('top_n_by_relative_volume', 10)} by relative volume · stop -{cfg.get('stop_loss_pct', 1.0):g}%, "
        f"trails {cfg.get('trail_pct', 1.0):g}% after +{cfg.get('profit_trigger_pct', 2.0):g}%",
        "",
    ]
    if engine.kill.tripped:
        lines.append(f"⛔ KILL SWITCH: {engine.kill.state.kill_reason}. No trading until tomorrow.")
    p = engine.position
    if p:
        lines.append(f"OPEN TRADE: {p.side.upper()} {p.qty:g} {p.symbol} @ ${p.entry_price:,.2f} · stop "
                     f"${p.stop:,.2f} · +2% trigger ${p.trigger:,.2f} · best ${p.best_price:,.2f}")
    else:
        lines.append("OPEN TRADE: none")
    lines.append(f"Realized P&L ${engine.ledger['realized_pnl_total']:+,.2f} · trades {engine.ledger['trades']} "
                 f"· wins {engine.ledger['wins']}")
    lines.append("")

    scan = engine.last_scan
    if not scan:
        lines.append("Waiting for the first scan…" if is_regular_hours(now) else "Market closed. Scanning starts at 9:30 ET.")
    else:
        lines.append(f"Scan {to_et(datetime.fromisoformat(scan['ts'])):%H:%M} · data for {scan['symbols_with_data']}"
                     f"/{scan['universe']} stocks")
        header = f"{'Symbol':<7}{'Price':>10}{'RelVol':>8}{'RSI':>7}{'50/200':>8}  " + " ".join(f"{t:^6}" for _, t in COLS) + "  Result"
        lines += [header, "-" * len(header)]
        for r in scan["rows"]:
            rsi_txt = "-" if r["rsi"] is None else f"{r['rsi']:.1f}"
            result = ("READY → " + ("BUY" if r["signal"] == "long" else "SHORT")) if r["ready"] else (
                (r["signal"] or "").upper() + " blocked" if r["signal"] else "no signal")
            lines.append(f"{r['symbol']:<7}{r['price']:>10,.2f}{r['rel_vol']:>7.1f}x{rsi_txt:>7}{(r['trend'] or '-'):>8}  "
                         + " ".join(f"{_mark(r, k):^6}" for k, _ in COLS) + f"  {result}")
    recent = engine.signals.recent(5)
    if recent:
        lines += ["", "Latest signals (website ledger, $1,000 each):"]
        for s in recent:
            lines.append(f"  {to_et(datetime.fromisoformat(s['ts'])):%H:%M} {s['side'].upper():<5} {s['symbol']:<6} "
                         f"@ ${s['price']:,.2f} · {s['status']} · {s['pnl_pct']:+.2f}%"
                         + (f" ({s['exit_reason']})" if s['exit_reason'] else ""))
    return "\n".join(lines)
