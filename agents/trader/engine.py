"""Trader engine: runs the strategy spec one closed minute at a time.

Call step(now) once per minute. Everything the engine decides goes to the
decision log with timestamp, signal values, reasoning, entry/exit and P&L.
"""
import json
import select
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from shared.market_calendar import is_regular_hours, to_et, trading_days_between
from shared.notifications import format_trade_alert
from shared.risk import MARGIN_MIN_EQUITY_USD, KillSwitch, effective_limits

from agents.trader import strategy
from agents.trader.indicators import ema, relative_volume, rsi
from agents.trader.strategy import LONG, SHORT


@dataclass
class Position:
    symbol: str
    side: str
    qty: float
    entry_price: float
    entry_ts: str
    entry_day: str
    trigger: float              # +2% level: stop starts moving (or fixed take-profit)
    stop: float                 # current stop; only ever moves in the position's favour
    initial_stop: float
    best_price: float           # best price seen since entry (high for longs, low for shorts)
    entry_rsi: float
    entry_rel_vol: float
    order_id: str


class AutoApprover:
    """Used for dry run and replay: no real money, nothing to approve."""
    def approve(self, summary: str) -> bool:
        return True


class ConsoleApprover:
    """Shakedown supervision: a human types 'y' at the terminal for every live entry.
    No answer within the timeout means NO."""
    def __init__(self, timeout_s: int):
        self.timeout_s = timeout_s

    def approve(self, summary: str) -> bool:
        print(f"\nAPPROVE LIVE ORDER? {summary}\nType y + Enter within {self.timeout_s}s: ", end="", flush=True)
        ready, _, _ = select.select([sys.stdin], [], [], self.timeout_s)
        return bool(ready) and sys.stdin.readline().strip().lower() == "y"


class TraderEngine:
    def __init__(self, cfg: dict, broker, feed, universe: list[str], state_dir: Path,
                 decisions, logger, notifier, approver=None, reporter=None):
        self.cfg = cfg
        self.limits = effective_limits(cfg)
        self.after_trigger = cfg.get("after_profit_trigger", "trail")
        if self.after_trigger not in strategy.AFTER_TRIGGER_MODES:
            raise ValueError(f"after_profit_trigger must be one of {strategy.AFTER_TRIGGER_MODES}")
        self.broker, self.feed, self.universe = broker, feed, universe
        self.decisions, self.logger, self.notifier = decisions, logger, notifier
        self.reporter = reporter
        self.supervised = broker.live and cfg.get("phase", "shakedown") == "shakedown"
        self.approver = approver or (ConsoleApprover(cfg.get("approval_timeout_seconds", 60))
                                     if self.supervised else AutoApprover())
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.position_path = self.state_dir / "position.json"
        self.ledger_path = self.state_dir / "ledger.json"
        self.kill = KillSwitch(self.limits["daily_loss_limit_pct"], self.state_dir / "killswitch.json")
        self.position = self._load_position()
        self.ledger = json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else {
            "day": None, "tasks_today": 0, "realized_pnl_total": 0.0, "trades": 0, "wins": 0}
        self.last_status = "idle"
        self.last_task = ""
        self._last_checked_bar: str | None = None

    # ---- persistence -------------------------------------------------
    def _load_position(self) -> Position | None:
        if self.position_path.exists():
            return Position(**json.loads(self.position_path.read_text()))
        return None

    def _save(self) -> None:
        if self.position:
            self.position_path.write_text(json.dumps(asdict(self.position)))
        elif self.position_path.exists():
            self.position_path.unlink()
        self.ledger_path.write_text(json.dumps(self.ledger))

    # ---- helpers -----------------------------------------------------
    def _effective_mode(self, equity: float) -> str:
        if self.limits["mode"] == "margin" and equity < MARGIN_MIN_EQUITY_USD:
            return "cash"
        return self.limits["mode"]

    def _marks(self, now: datetime) -> dict[str, float]:
        if not self.position:
            return {}
        bars = self.feed.minute_bars(self.position.symbol, now)
        return {self.position.symbol: bars[-1].close} if bars else {self.position.symbol: self.position.entry_price}

    def _report(self, status: str, task: str, equity: float | None = None) -> None:
        self.last_status, self.last_task = status, task
        if self.reporter:
            pnl = (equity - self.limits["account_size_usd"]) if equity is not None else self.ledger["realized_pnl_total"]
            self.reporter(status, task, self.ledger["tasks_today"], pnl)

    # ---- main loop step ----------------------------------------------
    def step(self, now: datetime) -> None:
        try:
            self._step(now)
        finally:
            self._save()

    def _step(self, now: datetime) -> None:
        if not is_regular_hours(now):
            self._report("sleeping", "market closed")
            return
        today = to_et(now).date()
        if self.ledger["day"] != today.isoformat():
            self.ledger.update(day=today.isoformat(), tasks_today=0)

        acct = self.broker.account(self._marks(now))
        # Day P&L is measured from the previous session's last equity, so an
        # overnight gap on a carried position counts toward the -3% limit.
        self.kill.start_day(today, self.ledger.get("last_equity") or acct.equity)
        self.ledger["last_equity"] = round(acct.equity, 2)

        if self.kill.tripped:
            self._report("halted", f"kill switch: {self.kill.state.kill_reason}", acct.equity)
            return
        if self.kill.check(acct.equity):
            self.decisions.record("kill_switch", self.kill.state.kill_reason, at=now,
                                  equity=round(acct.equity, 2), start_equity=self.kill.state.start_equity)
            self.notifier.trade_alert(f"⛔ KILL SWITCH {self.kill.state.kill_reason}. No more trading today.")
            if self.position and self.cfg.get("kill_switch_flattens_position", True):
                bars = self.feed.minute_bars(self.position.symbol, now)
                self._exit(now, "kill_switch", bars[-1].close if bars else self.position.entry_price)
            self._report("halted", "kill switch tripped", acct.equity)
            return

        if self.position:
            self._manage_position(now, today)
        else:
            if acct.margin_deficit > 0:
                self.decisions.record("skip_entries", "broker reports an intraday margin deficit; no new entries until it clears",
                                      at=now, margin_deficit=acct.margin_deficit)
                self._report("watching", "margin deficit, entries paused", acct.equity)
                return
            self._scan_and_enter(now, today, acct)

    # ---- exits -------------------------------------------------------
    def _manage_position(self, now: datetime, today) -> None:
        p = self.position
        bars = self.feed.minute_bars(p.symbol, now)
        entry_ts = datetime.fromisoformat(p.entry_ts)
        for bar in bars:
            if bar.ts < entry_ts or (self._last_checked_bar and bar.ts.isoformat() <= self._last_checked_bar):
                continue
            self._last_checked_bar = bar.ts.isoformat()
            # Check the stop as it stood when the bar opened, then ratchet it from the bar's extreme.
            target = p.trigger if self.after_trigger == "take_profit" else None
            hit = strategy.bar_exit(p.side, bar.open, bar.high, bar.low, p.stop, target)
            if hit:
                self._exit(now, hit[0], hit[1])
                return
            p.best_price = max(p.best_price, bar.high) if p.side == LONG else min(p.best_price, bar.low)
            new_stop = round(strategy.ratchet_stop(p.side, p.entry_price, p.best_price, p.stop, p.trigger,
                                                   self.after_trigger, float(self.cfg.get("trail_pct", 1.0))), 4)
            if new_stop != p.stop:
                self.decisions.record("stop_moved", f"price reached the +{self.cfg.get('profit_trigger_pct', 2.0):g}% "
                                      f"trigger; stop {self.after_trigger}", at=now, symbol=p.symbol, side=p.side,
                                      old_stop=p.stop, new_stop=new_stop, best_price=p.best_price,
                                      entry_price=p.entry_price)
                p.stop = new_stop
        held = trading_days_between(date.fromisoformat(p.entry_day), today)
        if held >= int(self.cfg.get("max_hold_days", 5)) and bars:
            self._exit(now, f"max_hold_days ({held} >= {self.cfg.get('max_hold_days', 5)})", bars[-1].close)
            return
        last = bars[-1].close if bars else p.entry_price
        self._report("in_position", f"{p.side.upper()} {p.symbol} {p.qty:g} @ {p.entry_price:.2f}, last {last:.2f}")

    def _exit(self, now: datetime, reason: str, ref_price: float) -> None:
        p = self.position
        side = "sell" if p.side == LONG else "buy_to_cover"
        fill = self.broker.place_market_order(p.symbol, side, p.qty, ref_price, now)
        direction = 1 if p.side == LONG else -1
        pnl = direction * (fill.price - p.entry_price) * p.qty
        pnl_pct = direction * (fill.price / p.entry_price - 1) * 100
        self.ledger["realized_pnl_total"] = round(self.ledger["realized_pnl_total"] + pnl, 2)
        self.ledger["trades"] += 1
        self.ledger["wins"] += int(pnl > 0)
        self.ledger["tasks_today"] += 1
        self.decisions.record(
            "exit", reason, at=now, symbol=p.symbol, side=p.side, qty=p.qty,
            entry_price=p.entry_price, exit_price=fill.price, reference_price=round(ref_price, 4),
            slippage_vs_reference=round(fill.price - ref_price, 4), pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct, 3), order_id=fill.order_id, broker=self.broker.name,
            realized_pnl_total=self.ledger["realized_pnl_total"])
        self.notifier.trade_alert(format_trade_alert(
            "SELL" if p.side == LONG else "COVER", p.symbol, fill.price, reason=reason, pnl=pnl))
        self.position = None
        self._last_checked_bar = None
        self._save()
        self._report("idle", f"closed {p.symbol}: {reason}, P&L {pnl:+.2f}")

    # ---- entries -----------------------------------------------------
    def _scan_and_enter(self, now: datetime, today, acct) -> None:
        cfg = self.cfg
        lookback = int(cfg.get("relative_volume_lookback_days", 20))
        rel_vols, bars_by_symbol = {}, {}
        for sym in self.universe:
            bars = self.feed.minute_bars(sym, now)
            prior = self.feed.prior_daily_volumes(sym, today, lookback)
            if not bars or len(prior) < lookback:
                continue
            rv = relative_volume(sum(b.volume for b in bars), prior)
            if rv is not None:
                rel_vols[sym], bars_by_symbol[sym] = rv, bars

        top = strategy.top_by_relative_volume(rel_vols, int(cfg.get("top_n_by_relative_volume", 10)))
        period = int(cfg.get("rsi_period", 14))
        scan = []
        for sym, rv in top:
            value = rsi([b.close for b in bars_by_symbol[sym]], period)
            signal = strategy.entry_signal(value, cfg.get("rsi_short_at_or_above", 80), cfg.get("rsi_long_at_or_below", 15))
            scan.append({"symbol": sym, "rel_vol": round(rv, 3), "rsi": None if value is None else round(value, 2),
                         "signal": signal, "price": bars_by_symbol[sym][-1].close})
        signals = [s for s in scan if s["signal"]]
        if signals and self.cfg.get("trend_filter_enabled", True):
            fast_n, slow_n = int(cfg.get("trend_fast_ema", 50)), int(cfg.get("trend_slow_ema", 200))
            lookback_days = int(cfg.get("trend_lookback_days", 252))
            for s in signals:
                closes = self.feed.prior_daily_closes(s["symbol"], today, lookback_days)
                fast, slow = ema(closes, fast_n), ema(closes, slow_n)
                s["ema_fast"] = None if fast is None else round(fast, 4)
                s["ema_slow"] = None if slow is None else round(slow, 4)
                s["trend_ok"] = strategy.trend_allows(s["signal"], fast, slow)
        self.decisions.record("scan", f"{len(rel_vols)} symbols with data; top {len(top)} by relative volume; "
                              f"{len(signals)} signal(s)", at=now, top=scan)
        self._report("watching", f"scanned top {len(top)}, {len(signals)} signal(s)", acct.equity)

        mode = self._effective_mode(acct.equity)
        for s in signals:  # already ordered by relative volume, highest first
            if s.get("trend_ok") is False:
                need = "50 EMA below 200 EMA" if s["signal"] == SHORT else "50 EMA above 200 EMA"
                self.decisions.record("skip_signal", f"{s['signal'].upper()} skipped: daily trend filter needs {need}"
                                      + ("" if s["ema_slow"] is not None else " (not enough daily history)"), at=now, **s)
                continue
            if s["signal"] == SHORT:
                if mode != "margin" or acct.account_type != "margin":
                    why = ("cash mode is long-only" if self.limits["mode"] == "cash"
                           else f"margin mode needs ${MARGIN_MIN_EQUITY_USD:,.0f}+ equity in a margin account "
                                f"(equity ${acct.equity:,.2f}, account {acct.account_type})")
                    self.decisions.record("skip_signal", f"SHORT skipped: {why}", at=now, **s)
                    continue
                if self.broker.is_easy_to_borrow(s["symbol"]) is not True:
                    self.decisions.record("skip_signal", "SHORT skipped: not confirmed easy-to-borrow", at=now, **s)
                    continue
            if self._enter(now, s, acct, mode):
                return

    def _enter(self, now: datetime, s: dict, acct, mode: str) -> bool:
        cfg = self.cfg
        # Cash mode: only settled cash (no good-faith violations). Margin: never more than equity (no leverage).
        available = acct.settled_cash if mode == "cash" else (min(acct.equity, acct.cash) if s["signal"] == LONG else acct.equity)
        qty = strategy.position_size(self.limits["account_size_usd"], self.limits["risk_per_trade_pct"],
                                     float(cfg.get("stop_loss_pct", 1.0)), s["price"], max(available, 0.0),
                                     bool(cfg.get("allow_fractional_shares", False)))
        if qty <= 0:
            self.decisions.record("skip_signal", f"size is 0 shares (available ${available:,.2f} "
                                  f"{'settled cash' if mode == 'cash' else 'equity'}, price ${s['price']:,.2f})", at=now, **s)
            return False
        side = "buy" if s["signal"] == LONG else "sell_short"
        summary = (f"{side.upper()} {qty:g} {s['symbol']} ~${s['price']:,.2f} (~${qty * s['price']:,.2f}) "
                   f"RSI {s['rsi']} relVol {s['rel_vol']}x")
        if not self.approver.approve(summary):
            self.decisions.record("entry_not_approved", "human did not approve within the timeout", at=now, **s)
            return False
        fill = self.broker.place_market_order(s["symbol"], side, qty, s["price"], now)
        trigger, stop = strategy.initial_levels(fill.price, s["signal"], float(cfg.get("profit_trigger_pct", 2.0)),
                                                float(cfg.get("stop_loss_pct", 1.0)))
        self.position = Position(s["symbol"], s["signal"], qty, fill.price, now.isoformat(),
                                 to_et(now).date().isoformat(), round(trigger, 4), round(stop, 4), round(stop, 4),
                                 fill.price, s["rsi"], s["rel_vol"], fill.order_id)
        self.ledger["tasks_today"] += 1
        self._save()
        self.decisions.record(
            "entry", f"RSI {s['rsi']} {'>= ' + str(cfg.get('rsi_short_at_or_above', 80)) if s['signal'] == SHORT else '<= ' + str(cfg.get('rsi_long_at_or_below', 15))} "
                     f"on a top-{cfg.get('top_n_by_relative_volume', 10)} relative-volume name",
            at=now, symbol=s["symbol"], side=s["signal"], qty=qty, entry_price=fill.price,
            reference_price=s["price"], slippage_vs_reference=round(fill.price - s["price"], 4),
            profit_trigger=round(trigger, 4), stop=round(stop, 4), after_trigger=self.after_trigger,
            rsi=s["rsi"], rel_vol=s["rel_vol"], ema_fast=s.get("ema_fast"), ema_slow=s.get("ema_slow"),
            mode=mode, order_id=fill.order_id, broker=self.broker.name, supervised=self.supervised)
        self.notifier.trade_alert(format_trade_alert(
            "BUY" if s["signal"] == LONG else "SHORT", s["symbol"], fill.price, s["rsi"], s["rel_vol"],
            cfg.get("profit_trigger_pct", 2.0), cfg.get("stop_loss_pct", 1.0)))
        self._report("in_position", f"entered {s['signal']} {s['symbol']}")
        return True

    def status(self) -> dict:
        return {
            "status": self.last_status, "current_task": self.last_task,
            "position": asdict(self.position) if self.position else None,
            "kill_switch": asdict(self.kill.state) if self.kill.state else None,
            "ledger": self.ledger, "limits": self.limits, "broker": self.broker.name,
            "supervised": self.supervised,
        }
