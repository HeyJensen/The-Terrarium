"""Brokers the engine can route orders to.

SimBroker      in-memory fills with slippage and T+1 settlement. Used for
               replay tests and for dry-run shakedown. Never touches money.
RobinhoodMCPBroker  the live route. NOT implemented: Robinhood's agentic
               access is an MCP server with browser OAuth and no documented
               request/response schema, and talking to it from Python needs a
               new dependency (an MCP client). That needs the human's OK.
               Until then it refuses every call.
"""
import itertools
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime

from shared.market_calendar import next_trading_day, to_et

SIDES = ("buy", "sell", "sell_short", "buy_to_cover")


@dataclass
class Account:
    account_type: str           # "cash" or "margin"
    equity: float
    cash: float
    settled_cash: float         # cash usable without a good-faith violation
    margin_deficit: float = 0.0 # broker-reported intraday margin deficit, if any


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: str
    qty: float
    price: float
    ts: datetime


@dataclass
class Quote:
    bid: float
    ask: float


@dataclass
class OrderStatus:
    open: bool
    filled_qty: float
    avg_price: float


class Broker(ABC):
    name = "abstract"
    live = False

    @abstractmethod
    def account(self, marks: dict[str, float]) -> Account: ...

    @abstractmethod
    def positions(self) -> dict[str, float]:
        """symbol -> signed quantity (negative = short)."""

    @abstractmethod
    def place_market_order(self, symbol: str, side: str, qty: float, ref_price: float, now: datetime) -> Fill: ...

    @abstractmethod
    def is_easy_to_borrow(self, symbol: str) -> bool | None:
        """True/False if known, None if the broker can't say (treated as NOT borrowable)."""

    @abstractmethod
    def quote(self, symbol: str, ref_price: float) -> Quote: ...

    @abstractmethod
    def place_limit_order(self, symbol: str, side: str, qty: float, limit: float, now: datetime) -> str:
        """Day limit order; returns an order id."""

    @abstractmethod
    def order_status(self, order_id: str) -> OrderStatus: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> None: ...


@dataclass
class _Unsettled:
    amount: float
    settles_on: date


class SimBroker(Broker):
    name = "sim"
    live = False

    def __init__(self, starting_cash: float, account_type: str = "cash", slippage_bps: float = 5.0,
                 hard_to_borrow: set[str] | None = None, spread_bps: float = 4.0, price_source=None,
                 passive_fills: str = "assume"):
        self.account_type = account_type
        self.cash = starting_cash
        self.slippage = slippage_bps / 10_000
        self.hard_to_borrow = hard_to_borrow or set()
        self._positions: dict[str, float] = {}
        self._unsettled: list[_Unsettled] = []
        self._ids = itertools.count(1)
        self.fills: list[Fill] = []
        self._today: date | None = None
        self.spread = spread_bps / 10_000
        # price_source(symbol, now) -> (low, high) of the latest bar. Lets a resting limit order
        # fill only if price actually traded through it; without it, only marketable limits fill.
        self.price_source = price_source
        # How a resting (non-marketable) limit is treated, e.g. a buy at the bid. A simulator can't know
        # whether someone sells to our bid in the next 10 seconds, so this is an explicit assumption:
        #   assume  fills at the limit (optimistic; the shakedown measures the real fill rate)
        #   touch   fills only if the latest bar traded through the limit (needs price_source)
        #   never   never fills (tests the cancel path)
        if passive_fills not in ("assume", "touch", "never"):
            raise ValueError("passive_fills must be assume, touch or never")
        self.passive_fills = passive_fills
        self._orders: dict[str, OrderStatus] = {}
        self._last_ref: dict[str, float] = {}

    def _settle(self, today: date) -> None:
        self._unsettled = [u for u in self._unsettled if u.settles_on > today]

    def account(self, marks: dict[str, float]) -> Account:
        if self._today:
            self._settle(self._today)
        unsettled = sum(u.amount for u in self._unsettled)
        equity = self.cash + sum(q * marks.get(s, 0.0) for s, q in self._positions.items())
        return Account(self.account_type, equity, self.cash, self.cash - unsettled)

    def positions(self) -> dict[str, float]:
        return dict(self._positions)

    def quote(self, symbol, ref_price):
        self._last_ref[symbol] = ref_price
        return Quote(round(ref_price * (1 - self.spread / 2), 4), round(ref_price * (1 + self.spread / 2), 4))

    def place_limit_order(self, symbol, side, qty, limit, now):
        """Simulated fill: a marketable limit (buy >= ask, sell <= bid) fills in full at its price;
        a resting one follows `passive_fills`. Queue position and partial fills aren't modelled."""
        q = self.quote(symbol, self._last_ref.get(symbol, limit))
        buying = side in ("buy", "buy_to_cover")
        marketable = limit >= q.ask if buying else limit <= q.bid
        if marketable or self.passive_fills == "assume":
            fills = True
        elif self.passive_fills == "touch" and self.price_source:
            low_high = self.price_source(symbol, now)
            fills = low_high is not None and (low_high[0] < limit if buying else low_high[1] > limit)
        else:
            fills = False
        order_id = f"sim-limit-{len(self._orders) + 1}"
        if fills:
            saved, self.slippage = self.slippage, 0.0
            try:
                fill = self.place_market_order(symbol, side, qty, limit, now)
            finally:
                self.slippage = saved
            self._orders[order_id] = OrderStatus(False, fill.qty, fill.price)
        else:
            self._orders[order_id] = OrderStatus(True, 0.0, 0.0)
        return order_id

    def order_status(self, order_id):
        return self._orders[order_id]

    def cancel_order(self, order_id):
        st = self._orders[order_id]
        self._orders[order_id] = OrderStatus(False, st.filled_qty, st.avg_price)

    def place_market_order(self, symbol, side, qty, ref_price, now):
        if side not in SIDES or qty <= 0:
            raise ValueError(f"bad order {side} {qty}")
        if side == "sell_short" and self.account_type != "margin":
            raise ValueError("short selling requires a margin account")
        self._today = to_et(now).date()
        self._settle(self._today)
        buying = side in ("buy", "buy_to_cover")
        price = ref_price * (1 + self.slippage) if buying else ref_price * (1 - self.slippage)
        notional = price * qty
        if side == "buy" and self.account_type == "cash":
            if notional > self.account(marks={}).settled_cash + 1e-9:
                raise ValueError("insufficient settled cash (would be a good-faith violation)")
        signed = qty if buying else -qty
        self.cash -= signed * price
        if side == "sell" and self.account_type == "cash":
            self._unsettled.append(_Unsettled(notional, next_trading_day(self._today)))
        new_qty = self._positions.get(symbol, 0.0) + signed
        if abs(new_qty) < 1e-9:
            self._positions.pop(symbol, None)
        else:
            self._positions[symbol] = new_qty
        fill = Fill(f"sim-{next(self._ids)}", symbol, side, qty, round(price, 4), now)
        self.fills.append(fill)
        return fill

    def is_easy_to_borrow(self, symbol):
        return symbol not in self.hard_to_borrow

    # Dry-run state survives restarts so it stays in step with the engine's position file.
    def save(self, path) -> None:
        path.write_text(json.dumps({
            "account_type": self.account_type, "cash": self.cash, "positions": self._positions,
            "unsettled": [[u.amount, u.settles_on.isoformat()] for u in self._unsettled],
            "next_id": next(self._ids), "today": self._today.isoformat() if self._today else None}))

    @classmethod
    def load_or_new(cls, path, starting_cash: float, account_type: str, slippage_bps: float) -> "SimBroker":
        b = cls(starting_cash, account_type, slippage_bps)
        if path.exists():
            d = json.loads(path.read_text())
            b.cash, b._positions = d["cash"], d["positions"]
            b._unsettled = [_Unsettled(a, date.fromisoformat(s)) for a, s in d["unsettled"]]
            b._ids = itertools.count(d["next_id"])
            b._today = date.fromisoformat(d["today"]) if d["today"] else None
        return b


class RobinhoodMCPBroker(Broker):
    """Placeholder for the live Robinhood Agentic Trading route.

    Implementing this needs (1) the human's approval to add an MCP client
    dependency, (2) the OAuth token from docs/robinhood-oauth-setup.md, and
    (3) confirming the tool schemas (place_equity_order, get_accounts,
    get_equity_positions, get_equity_quotes) against the live server.
    """
    name = "robinhood-mcp"
    live = True

    def _refuse(self, *_, **__):
        raise NotImplementedError(
            "Live Robinhood routing is not wired yet. See docs/robinhood-oauth-setup.md; "
            "the MCP client dependency needs the human's approval first.")

    account = positions = place_market_order = is_easy_to_borrow = _refuse
    quote = place_limit_order = order_status = cancel_order = _refuse
