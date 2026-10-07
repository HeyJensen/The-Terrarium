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


@dataclass
class _Unsettled:
    amount: float
    settles_on: date


class SimBroker(Broker):
    name = "sim"
    live = False

    def __init__(self, starting_cash: float, account_type: str = "cash", slippage_bps: float = 5.0,
                 hard_to_borrow: set[str] | None = None):
        self.account_type = account_type
        self.cash = starting_cash
        self.slippage = slippage_bps / 10_000
        self.hard_to_borrow = hard_to_borrow or set()
        self._positions: dict[str, float] = {}
        self._unsettled: list[_Unsettled] = []
        self._ids = itertools.count(1)
        self.fills: list[Fill] = []
        self._today: date | None = None

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
