"""Trader engine: runs the strategy spec one closed minute at a time.

Call step(now) once per minute. Everything the engine decides goes to the
decision log with timestamp, signal values, reasoning, entry/exit and P&L.
"""
import json
import select
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from shared.market_calendar import is_regular_hours, to_et, trading_days_between
from shared.notifications import format_trade_alert
from shared.risk import MARGIN_MIN_EQUITY_USD, KillSwitch, effective_limits

from agents.trader import strategy
from agents.trader.broker import Fill
from agents.trader.indicators import ema, relative_volume, rsi
from agents.trader.signal_ledger import SignalLedger
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


def _slim(row: dict) -> dict:
    """Row fields worth logging on a skip (drops the nested checks dict)."""
    return {k: v for k, v in row.items() if k not in ("checks", "side_why", "ready", "strategy_signal")}


class TraderEngine:
    def __init__(self, cfg: dict, broker, feed, universe: list[str], state_dir: Path,
                 decisions, logger, notifier, approver=None, reporter=None, signal_sink=None, sleep=None):
        self.cfg = cfg
        self.limits = effective_limits(cfg)
        self.after_trigger = cfg.get("after_profit_trigger", "trail")
        if self.after_trigger not in strategy.AFTER_TRIGGER_MODES:
            raise ValueError(f"after_profit_trigger must be one of {strategy.AFTER_TRIGGER_MODES}")
        self.broker, self.feed, self.universe = broker, feed, universe
        self.decisions, self.logger, self.notifier = decisions, logger, notifier
        self.reporter = reporter
        self.signal_sink = signal_sink
        self.sleep = sleep or (lambda s: None)
        self.last_scan: dict | None = None
        self._active_signals: set = set()
        self.supervised = broker.live and cfg.get("phase", "shakedown") == "shakedown"
        self.approver = approver or (ConsoleApprover(cfg.get("approval_timeout_seconds", 60))
                                     if self.supervised else AutoApprover())
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.position_path = self.state_dir / "position.json"
        self.ledger_path = self.state_dir / "ledger.json"
        self.signals = SignalLedger(self.state_dir / "signals.json", cfg)
        self.kill = KillSwitch(self.limits["daily_loss_limit_pct"], self.state_dir / "killswitch.json")
        self.position = self._load_position()
        self.ledger = json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else {
            "day": None, "tasks_today": 0, "realized_pnl_total": 0.0, "trades": 0, "wins": 0}
        self.last_status = "idle"
        self.last_task = ""
        self._last_checked_bar: str | None = None
        self._last_top: list[str] = []

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
        self.feed.prepare(now, hot=self._last_top + ([self.position.symbol] if self.position else [])
                          + self.signals.open_symbols())
        try:
            self._trade_step(now)
        finally:
            self.signals.update(self.feed, now)
            self.signals.save()
            if self.signal_sink:
                self.signal_sink(self.signals.recent())

    def _trade_step(self, now: datetime) -> None:
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
        # Scan every minute, even while holding, so the console and the signal feed stay live.
        rows = self._scan(now, today, acct)
        if self.position:
            return
        if acct.margin_deficit > 0:
            self.decisions.record("skip_entries", "broker reports an intraday margin deficit; no new entries until it clears",
                                  at=now, margin_deficit=acct.margin_deficit)
            self._report("watching", "margin deficit, entries paused", acct.equity)
            return
        self._report("watching", f"scanned top {len(rows)}, {sum(1 for r in rows if r['ready'])} ready", acct.equity)
        self._try_entries(now, rows, acct)

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
    def _scan(self, now: datetime, today, acct) -> list[dict]:
        """Rank, compute indicators, and check every requirement for the top-N names.
        Each row says which requirement passed, so the console can show it."""
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
        self._last_top = [sym for sym, _ in top]
        period = int(cfg.get("rsi_period", 14))
        short_at, long_at = cfg.get("rsi_short_at_or_above", 85), cfg.get("rsi_long_at_or_below", 15)
        fast_n, slow_n = int(cfg.get("trend_fast_ema", 50)), int(cfg.get("trend_slow_ema", 200))
        trend_days = int(cfg.get("trend_lookback_days", 252))
        max_age_s = float(cfg.get("max_bar_age_minutes", 3)) * 60
        mode = self._effective_mode(acct.equity)
        rows = []
        for sym, rv in top:
            bars = bars_by_symbol[sym]
            value = rsi([b.close for b in bars], period)
            signal = strategy.entry_signal(value, short_at, long_at)
            closes = self.feed.prior_daily_closes(sym, today, trend_days)
            fast, slow = ema(closes, fast_n), ema(closes, slow_n)
            age_s = round((now - (bars[-1].ts + timedelta(minutes=1))).total_seconds())
            row = {"symbol": sym, "price": bars[-1].close, "rel_vol": round(rv, 3),
                   "rsi": None if value is None else round(value, 2), "signal": signal,
                   "ema_fast": None if fast is None else round(fast, 4),
                   "ema_slow": None if slow is None else round(slow, 4),
                   "trend": None if fast is None or slow is None else ("up" if fast > slow else "down"),
                   "bar_age_s": age_s}
            trend_ok = True
            if signal and cfg.get("trend_filter_enabled", True):
                trend_ok = strategy.trend_allows(signal, fast, slow)
            side_ok, side_why = True, ""
            if signal == SHORT:
                if mode != "margin" or acct.account_type != "margin":
                    side_ok = False
                    side_why = ("cash mode is long-only" if self.limits["mode"] == "cash"
                                else f"margin mode needs ${MARGIN_MIN_EQUITY_USD:,.0f}+ equity in a margin account "
                                     f"(equity ${acct.equity:,.2f}, account {acct.account_type})")
                elif self.broker.is_easy_to_borrow(sym) is not True:
                    side_ok, side_why = False, "not confirmed easy-to-borrow"
            row["checks"] = {
                "top_rel_vol": True,
                "rsi": signal is not None,
                "trend": bool(signal) and trend_ok,
                "fresh_data": age_s <= max_age_s,
                "side_allowed": bool(signal) and side_ok,
                "no_open_trade": self.position is None,
            }
            row["side_why"] = side_why
            row["strategy_signal"] = all(row["checks"][k] for k in ("rsi", "trend", "fresh_data"))
            row["ready"] = all(row["checks"].values())
            rows.append(row)
        self.last_scan = {"ts": now.isoformat(), "symbols_with_data": len(rel_vols),
                          "universe": len(self.universe), "mode": mode, "rows": rows}
        n_sig = sum(1 for r in rows if r["signal"])
        self.decisions.record("scan", f"{len(rel_vols)} symbols with data; top {len(top)} by relative volume; "
                              f"{n_sig} RSI signal(s)", at=now, top=rows)
        self._emit_new_signals(now, rows)
        return rows

    def _emit_new_signals(self, now: datetime, rows: list[dict]) -> None:
        """Every strategy signal (RSI + trend + fresh data), whether or not this bot can take it,
        goes into the signal ledger once, when it first appears. Price is the bid the bot is told to use."""
        active = {(r["symbol"], r["signal"]) for r in rows if r["strategy_signal"]}
        for r in rows:
            key = (r["symbol"], r["signal"])
            if key in active and key not in self._active_signals:
                bid = self.broker.quote(r["symbol"], r["price"]).bid
                entry = self.signals.add(now, r["symbol"], r["signal"], bid, r["rsi"], r["rel_vol"])
                self.decisions.record("signal", "strategy signal published to the ledger", at=now, **entry)
        self._active_signals = active

    def _try_entries(self, now: datetime, rows: list[dict], acct) -> None:
        for r in rows:  # ordered by relative volume, highest first
            if not r["signal"]:
                continue
            if not r["ready"]:
                failed = [k for k, ok in r["checks"].items() if not ok]
                if "no_open_trade" not in failed:  # don't spam the log while holding a position
                    self.decisions.record("skip_signal", f"{r['signal'].upper()} skipped: "
                                          + "; ".join(self._why(r, k) for k in failed), at=now, **_slim(r))
                continue
            if self._enter(now, r, acct, self.last_scan["mode"]):
                return

    @staticmethod
    def _why(r: dict, check: str) -> str:
        if check == "trend":
            need = "50 EMA below 200 EMA" if r["signal"] == SHORT else "50 EMA above 200 EMA"
            return f"daily trend filter needs {need}" + ("" if r["ema_slow"] is not None else " (not enough daily history)")
        if check == "fresh_data":
            return f"newest bar is {r['bar_age_s']}s old (stale data)"
        if check == "side_allowed":
            return r["side_why"]
        return check

    def _enter(self, now: datetime, s: dict, acct, mode: str) -> bool:
        cfg = self.cfg
        # Cash mode: only settled cash (no good-faith violations). Margin: never more than equity (no leverage).
        available = acct.settled_cash if mode == "cash" else (min(acct.equity, acct.cash) if s["signal"] == LONG else acct.equity)
        quote = self.broker.quote(s["symbol"], s["price"])
        limit = round(quote.bid, 2)
        qty = strategy.position_size(self.limits["account_size_usd"], self.limits["risk_per_trade_pct"],
                                     float(cfg.get("stop_loss_pct", 1.0)), limit, max(available, 0.0),
                                     bool(cfg.get("allow_fractional_shares", False)))
        if qty <= 0:
            self.decisions.record("skip_signal", f"size is 0 shares (available ${available:,.2f} "
                                  f"{'settled cash' if mode == 'cash' else 'equity'}, bid ${limit:,.2f})", at=now, **_slim(s))
            return False
        side = "buy" if s["signal"] == LONG else "sell_short"
        summary = (f"{side.upper()} {qty:g} {s['symbol']} LIMIT ${limit:,.2f} (bid) ~${qty * limit:,.2f} "
                   f"RSI {s['rsi']} relVol {s['rel_vol']}x")
        if not self.approver.approve(summary):
            self.decisions.record("entry_not_approved", "human did not approve within the timeout", at=now, **_slim(s))
            return False
        fill = self._limit_with_timeout(s["symbol"], side, qty, limit, now, quote)
        if fill is None:
            return False
        trigger, stop = strategy.initial_levels(fill.price, s["signal"], float(cfg.get("profit_trigger_pct", 2.0)),
                                                float(cfg.get("stop_loss_pct", 1.0)))
        self.position = Position(s["symbol"], s["signal"], fill.qty, fill.price, now.isoformat(),
                                 to_et(now).date().isoformat(), round(trigger, 4), round(stop, 4), round(stop, 4),
                                 fill.price, s["rsi"], s["rel_vol"], fill.order_id)
        self.ledger["tasks_today"] += 1
        self._save()
        rule = (f">= {cfg.get('rsi_short_at_or_above', 85)}" if s["signal"] == SHORT
                else f"<= {cfg.get('rsi_long_at_or_below', 15)}")
        self.decisions.record(
            "entry", f"RSI {s['rsi']} {rule}, trend {s.get('trend')}, top-{cfg.get('top_n_by_relative_volume', 10)} "
                     f"relative volume; limit at bid filled",
            at=now, symbol=s["symbol"], side=s["signal"], qty=fill.qty, requested_qty=qty, entry_price=fill.price,
            limit_price=limit, bid=quote.bid, ask=quote.ask, reference_price=s["price"],
            slippage_vs_reference=round(fill.price - s["price"], 4),
            profit_trigger=round(trigger, 4), stop=round(stop, 4), after_trigger=self.after_trigger,
            rsi=s["rsi"], rel_vol=s["rel_vol"], ema_fast=s.get("ema_fast"), ema_slow=s.get("ema_slow"),
            mode=mode, order_id=fill.order_id, broker=self.broker.name, supervised=self.supervised)
        self.notifier.trade_alert(format_trade_alert(
            "BUY" if s["signal"] == LONG else "SHORT", s["symbol"], fill.price, s["rsi"], s["rel_vol"],
            cfg.get("profit_trigger_pct", 2.0), cfg.get("stop_loss_pct", 1.0)))
        self._report("in_position", f"entered {s['signal']} {s['symbol']}")
        return True

    def _limit_with_timeout(self, symbol: str, side: str, qty: float, limit: float, now: datetime, quote):
        """Limit order at the bid; cancel whatever hasn't filled after `entry_order_timeout_seconds`.
        Returns the (possibly partial) fill, or None if nothing filled."""
        timeout = float(self.cfg.get("entry_order_timeout_seconds", 10))
        order_id = self.broker.place_limit_order(symbol, side, qty, limit, now)
        waited = 0.0
        status = self.broker.order_status(order_id)
        while status.open and waited < timeout:
            self.sleep(1.0)
            waited += 1.0
            status = self.broker.order_status(order_id)
        if status.open:
            self.broker.cancel_order(order_id)
            status = self.broker.order_status(order_id)
        if status.filled_qty <= 0:
            self.decisions.record("entry_unfilled", f"limit {side} at bid ${limit:,.2f} not filled within "
                                  f"{timeout:g}s; cancelled", at=now, symbol=symbol, side=side, qty=qty,
                                  limit_price=limit, bid=quote.bid, ask=quote.ask, order_id=order_id)
            return None
        if status.filled_qty < qty:
            self.decisions.record("entry_partial", f"{status.filled_qty:g} of {qty:g} filled; rest cancelled",
                                  at=now, symbol=symbol, order_id=order_id)
        return Fill(order_id, symbol, side, status.filled_qty, status.avg_price, now)

    def status(self) -> dict:
        return {
            "status": self.last_status, "current_task": self.last_task,
            "position": asdict(self.position) if self.position else None,
            "kill_switch": asdict(self.kill.state) if self.kill.state else None,
            "ledger": self.ledger, "limits": self.limits, "broker": self.broker.name,
            "supervised": self.supervised,
        }
