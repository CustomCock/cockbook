"""Paper-Trading-Engine: simuliertes Futures-Konto gegen die echten Orderbücher.

Reine Logik ohne asyncio/Netz – getestet mit festen Büchern und Trades. Der Dienst
(``service.py``) füttert sie mit Trades und ruft ``on_tick`` alle 100 ms auf.

Fill-Modell (Annahmen)
----------------------
- **Market** läuft durch das echte Buch der Börse (beste Preise zuerst) -> Slippage wie
  in echt, solange die eigene Order das Buch nicht selbst verschieben würde (tut sie nicht:
  das Buch der Börse kennt die Paper-Order nicht). Reicht das bekannte Buch nicht, wird
  abgelehnt statt einen Fantasiepreis zu nehmen. Taker-Gebühr.
- **Limit** stellt sich *hinten* in die Warteschlange ihres Preislevels: ``queue_ahead`` =
  Menge, die beim Platzieren schon dort lag. Trades auf genau diesem Preis (Aggressor von
  der Gegenseite) arbeiten die Warteschlange ab; erst danach wird die eigene Order (auch
  teilweise) gefüllt. Schrumpft das Level im Buch (Cancels), schrumpft ``queue_ahead`` mit –
  Annahme: gecancelt wird *vor* uns (optimistisch, aber ohne L3-Daten nicht unterscheidbar).
  Handelt der Markt durch den Preis (Trade jenseits des Limits) oder liegt die Gegenseite
  des Buchs auf/hinter dem Limit, ist die Order sicher dran -> komplett gefüllt. Maker-Gebühr.
  Ein Limit, das beim Platzieren schon "marketable" ist, wird wie in echt sofort gegen das
  Buch ausgeführt (bis zum Limitpreis, Taker), der Rest bleibt liegen.
- **Stop** (Stop-Market), **TP/SL** und **Liquidation** lösen am Mid aus (stabiler als der
  letzte Trade, den ein einzelner Ausreißer-Fill verschieben würde) und werden als Market
  ausgeführt.
- Positionen: One-Way-Modus (je Coin eine Netto-Position), **isolierte Margin**:
  Margin = Nominalwert / Hebel, Liquidationspreis aus Hebel und Wartungsmarge. Funding wird
  nicht simuliert.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal, Protocol

from cockbook.config import FeeConfig, PaperConfig
from cockbook.core.events import Side

ZERO = Decimal(0)
OrderSide = Literal["buy", "sell"]
OrderKind = Literal["market", "limit", "stop"]


class OrderRejectedError(Exception):
    """Order wird nicht angenommen; die Nachricht geht an den Nutzer."""


class MarketView(Protocol):
    """Was die Engine von einem Markt braucht (live: Aggregator der Börse)."""

    @property
    def exchange(self) -> str: ...
    @property
    def tick_size(self) -> Decimal: ...
    @property
    def step_size(self) -> Decimal: ...
    def bbo(self) -> tuple[Decimal, Decimal] | None: ...
    def level_qty(self, side: Side, price: Decimal) -> Decimal: ...
    def levels(self, side: Side) -> Iterable[tuple[Decimal, Decimal]]:
        """Level einer Seite, beste zuerst (Bid absteigend, Ask aufsteigend)."""
        ...


@dataclass(slots=True)
class Order:
    id: str
    symbol: str
    side: OrderSide
    kind: OrderKind
    qty: Decimal
    price: Decimal | None = None  # Limit
    trigger: Decimal | None = None  # Stop
    reduce_only: bool = False
    tp: Decimal | None = None  # Bracket: wird beim Fill an die Position gehängt
    sl: Decimal | None = None
    filled: Decimal = ZERO
    queue_ahead: Decimal = ZERO
    created_ns: int = 0

    @property
    def remaining(self) -> Decimal:
        return self.qty - self.filled


@dataclass(slots=True)
class Position:
    symbol: str
    qty: Decimal  # > 0 long, < 0 short
    entry: Decimal
    leverage: int
    opened_ns: int
    tp: Decimal | None = None
    sl: Decimal | None = None
    # laufender Trade (für das Journal)
    realized: Decimal = ZERO
    fees: Decimal = ZERO
    max_qty: Decimal = ZERO
    exit_notional: Decimal = ZERO
    exit_qty: Decimal = ZERO

    @property
    def is_long(self) -> bool:
        return self.qty > 0

    @property
    def margin(self) -> Decimal:
        return abs(self.qty) * self.entry / self.leverage

    def upnl(self, mark: Decimal) -> Decimal:
        return (mark - self.entry) * self.qty

    def liq_price(self, mmr: Decimal) -> Decimal:
        inv = Decimal(1) / self.leverage
        if self.is_long:
            return self.entry * (1 - inv + mmr)
        return self.entry * (1 + inv - mmr)


@dataclass(slots=True)
class Fill:
    ts_ns: int
    order_id: str
    symbol: str
    side: OrderSide
    qty: Decimal
    price: Decimal
    fee: Decimal
    liquidity: Literal["maker", "taker"]
    realized: Decimal
    reason: str


@dataclass(slots=True)
class ClosedTrade:
    symbol: str
    side: Literal["long", "short"]
    qty: Decimal
    entry: Decimal
    exit: Decimal
    pnl: Decimal  # nach Gebühren
    fees: Decimal
    opened_ns: int
    closed_ns: int
    reason: str


@dataclass(slots=True)
class Account:
    wallet: Decimal
    day: str = ""
    day_realized: Decimal = ZERO
    positions: dict[str, Position] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    leverage: dict[str, int] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    trades: list[ClosedTrade] = field(default_factory=list)
    next_id: int = 1


_MAX_FILLS = 500
_MAX_TRADES = 2000


def _round_step(v: Decimal, step: Decimal) -> Decimal:
    return (v / step).to_integral_value(rounding=ROUND_DOWN) * step


def _round_tick(v: Decimal, tick: Decimal) -> Decimal:
    return (v / tick).to_integral_value() * tick


class PaperEngine:
    def __init__(
        self,
        cfg: PaperConfig,
        markets: Callable[[str], MarketView | None],
        account: Account | None = None,
    ) -> None:
        self.cfg = cfg
        self.markets = markets
        self.acc = account or Account(wallet=cfg.start_balance)
        self.version = 0  # zählt jede Zustandsänderung (Speichern/Senden)
        self.messages: list[tuple[str, str]] = []  # (Stufe, Text) für den Nutzer

    # --- Hilfen ------------------------------------------------------------------------

    def _market(self, symbol: str) -> MarketView:
        m = self.markets(symbol)
        if m is None:
            raise OrderRejectedError(f"{symbol}: kein Orderbuch – Coin erst im Chart öffnen")
        if m.bbo() is None:
            raise OrderRejectedError(f"{symbol}: Orderbuch nicht synchron")
        return m

    def _fees(self, m: MarketView) -> FeeConfig:
        fee = self.cfg.fees.get(m.exchange)
        if fee is None:
            raise OrderRejectedError(f"keine Gebühren für {m.exchange} konfiguriert")
        return fee

    def _changed(self) -> None:
        self.version += 1

    def _say(self, level: str, text: str) -> None:
        self.messages.append((level, text))

    def leverage_of(self, symbol: str) -> int:
        pos = self.acc.positions.get(symbol)
        if pos is not None:
            return pos.leverage
        return self.acc.leverage.get(symbol, self.cfg.default_leverage)

    @staticmethod
    def mid(m: MarketView) -> Decimal | None:
        bbo = m.bbo()
        return (bbo[0] + bbo[1]) / 2 if bbo else None

    def used_margin(self) -> Decimal:
        return sum((p.margin for p in self.acc.positions.values()), ZERO)

    def order_margin(self) -> Decimal:
        """Margin, die offene (positionserhöhende) Orders reservieren."""
        total = ZERO
        for o in self.acc.orders.values():
            if o.reduce_only:
                continue
            price = o.price or o.trigger
            if price is not None:
                total += o.remaining * price / self.leverage_of(o.symbol)
        return total

    def available(self) -> Decimal:
        return self.acc.wallet - self.used_margin() - self.order_margin()

    def upnl(self) -> Decimal:
        total = ZERO
        for p in self.acc.positions.values():
            m = self.markets(p.symbol)
            mark = self.mid(m) if m is not None else None
            if mark is not None:
                total += p.upnl(mark)
        return total

    def _roll_day(self, now_ns: int) -> None:
        day = datetime.fromtimestamp(now_ns / 1e9, UTC).strftime("%Y-%m-%d")
        if day != self.acc.day:
            self.acc.day = day
            self.acc.day_realized = ZERO

    def _loss_limit_hit(self) -> bool:
        lim = self.cfg.daily_loss_limit_usd
        return lim > 0 and self.acc.day_realized + self.upnl() <= -lim

    def _increases(self, symbol: str, side: OrderSide, qty: Decimal) -> Decimal:
        """Anteil von ``qty``, der die Position vergrößert (Rest schließt/reduziert)."""
        pos = self.acc.positions.get(symbol)
        if pos is None:
            return qty
        same = (side == "buy") == pos.is_long
        return qty if same else max(ZERO, qty - abs(pos.qty))

    # --- Orders ------------------------------------------------------------------------

    def place(
        self,
        symbol: str,
        side: OrderSide,
        kind: OrderKind,
        qty: Decimal,
        *,
        price: Decimal | None = None,
        trigger: Decimal | None = None,
        reduce_only: bool = False,
        tp: Decimal | None = None,
        sl: Decimal | None = None,
        now_ns: int,
    ) -> Order:
        self._roll_day(now_ns)
        m = self._market(symbol)
        qty = _round_step(qty, m.step_size)
        if qty <= 0:
            raise OrderRejectedError(f"Menge zu klein (Schrittweite {m.step_size.normalize()})")
        if kind == "limit":
            if price is None or price <= 0:
                raise OrderRejectedError("Limit-Order braucht einen Preis")
            price = _round_tick(price, m.tick_size)
        if kind == "stop":
            if trigger is None or trigger <= 0:
                raise OrderRejectedError("Stop-Order braucht einen Auslösepreis")
            trigger = _round_tick(trigger, m.tick_size)
        bid, ask = m.bbo() or (ZERO, ZERO)
        mid = (bid + ask) / 2
        if (
            kind == "stop"
            and trigger is not None
            and ((side == "buy" and trigger <= mid) or (side == "sell" and trigger >= mid))
        ):
            raise OrderRejectedError(
                "Stop liegt auf der falschen Seite des Kurses – dafür Limit/Market nehmen"
            )
        tp = _round_tick(tp, m.tick_size) if tp is not None else None
        sl = _round_tick(sl, m.tick_size) if sl is not None else None
        self._check_bracket(side, price or trigger or mid, tp, sl)
        pos = self.acc.positions.get(symbol)
        if reduce_only:
            if pos is None or (side == "buy") == pos.is_long:
                raise OrderRejectedError("Reduce-Only: keine Gegenposition vorhanden")
            qty = min(qty, abs(pos.qty))
        else:
            self._check_risk(symbol, side, qty, price or trigger or (ask if side == "buy" else bid))
        o = Order(
            id=f"P{self.acc.next_id}", symbol=symbol, side=side, kind=kind, qty=qty,
            price=price, trigger=trigger, reduce_only=reduce_only, tp=tp, sl=sl,
            created_ns=now_ns,
        )  # fmt: skip
        self.acc.next_id += 1
        self._changed()
        if kind == "market":
            self._execute_market(o, m, now_ns, "Market")
            if o.filled == 0:
                raise OrderRejectedError("Buch zu dünn für diese Menge")
            return o
        if kind == "limit":
            assert price is not None
            marketable = (side == "buy" and price >= ask) or (side == "sell" and price <= bid)
            if marketable:
                self._execute_market(o, m, now_ns, "Limit (sofort)", limit=price)
                if o.remaining <= 0:
                    return o
            o.queue_ahead = m.level_qty(Side.BID if side == "buy" else Side.ASK, price)
        self.acc.orders[o.id] = o
        return o

    def _check_bracket(
        self, side: OrderSide, ref: Decimal, tp: Decimal | None, sl: Decimal | None
    ) -> None:
        long = side == "buy"
        if tp is not None and (tp <= ref if long else tp >= ref):
            raise OrderRejectedError(
                "Take-Profit muss bei Long über, bei Short unter dem Einstieg liegen"
            )
        if sl is not None and (sl >= ref if long else sl <= ref):
            raise OrderRejectedError(
                "Stop-Loss muss bei Long unter, bei Short über dem Einstieg liegen"
            )

    def _check_risk(self, symbol: str, side: OrderSide, qty: Decimal, price: Decimal) -> None:
        inc = self._increases(symbol, side, qty)
        if inc <= 0:
            return
        if self._loss_limit_hit():
            raise OrderRejectedError("Tagesverlust-Limit erreicht – nur noch Schließen möglich")
        pos = self.acc.positions.get(symbol)
        cur = abs(pos.qty) * price if pos is not None else ZERO
        if cur + inc * price > self.cfg.max_position_usd:
            raise OrderRejectedError(
                f"Position wäre größer als das Limit ({self.cfg.max_position_usd:,.0f} USDT)"
            )
        need = inc * price / self.leverage_of(symbol)
        if need > self.available():
            raise OrderRejectedError(
                f"Nicht genug Margin: braucht {need:,.2f}, frei {self.available():,.2f} USDT"
            )

    def cancel(self, order_id: str) -> None:
        if self.acc.orders.pop(order_id, None) is None:
            raise OrderRejectedError("Order nicht (mehr) offen")
        self._changed()

    def cancel_all(self, symbol: str | None = None) -> int:
        ids = [i for i, o in self.acc.orders.items() if symbol is None or o.symbol == symbol]
        for i in ids:
            del self.acc.orders[i]
        if ids:
            self._changed()
        return len(ids)

    def modify(self, order_id: str, *, price: Decimal | None = None,
               trigger: Decimal | None = None) -> None:  # fmt: skip
        """Order verschieben (Chart: Linie ziehen). Limit verliert dabei den Platz in der
        Warteschlange – wie an der Börse."""
        o = self.acc.orders.get(order_id)
        if o is None:
            raise OrderRejectedError("Order nicht (mehr) offen")
        m = self._market(o.symbol)
        bid, ask = m.bbo() or (ZERO, ZERO)
        if o.kind == "limit" and price is not None:
            price = _round_tick(price, m.tick_size)
            if (o.side == "buy" and price >= ask) or (o.side == "sell" and price <= bid):
                raise OrderRejectedError("Limit würde sofort ausgeführt – dafür Market nehmen")
            o.price = price
            o.queue_ahead = m.level_qty(Side.BID if o.side == "buy" else Side.ASK, price)
        elif o.kind == "stop" and trigger is not None:
            trigger = _round_tick(trigger, m.tick_size)
            mid = (bid + ask) / 2
            if (o.side == "buy" and trigger <= mid) or (o.side == "sell" and trigger >= mid):
                raise OrderRejectedError("Stop liegt auf der falschen Seite des Kurses")
            o.trigger = trigger
        self._changed()

    def set_leverage(self, symbol: str, lev: int) -> None:
        if not 1 <= lev <= self.cfg.max_leverage:
            raise OrderRejectedError(f"Hebel 1–{self.cfg.max_leverage}")
        if symbol in self.acc.positions:
            raise OrderRejectedError("Hebel nur ohne offene Position änderbar")
        self.acc.leverage[symbol] = lev
        self._changed()

    def set_tpsl(self, symbol: str, tp: Decimal | None, sl: Decimal | None) -> None:
        pos = self.acc.positions.get(symbol)
        if pos is None:
            raise OrderRejectedError("Keine offene Position")
        m = self._market(symbol)
        mark = self.mid(m) or pos.entry
        tp = _round_tick(tp, m.tick_size) if tp is not None else None
        sl = _round_tick(sl, m.tick_size) if sl is not None else None
        # gegen den aktuellen Kurs prüfen: ein TP unter dem Kurs (Long) löste sofort aus
        self._check_bracket("buy" if pos.is_long else "sell", mark, tp, sl)
        pos.tp, pos.sl = tp, sl
        self._changed()

    def close(self, symbol: str, now_ns: int, reason: str = "Schließen") -> None:
        pos = self.acc.positions.get(symbol)
        if pos is None:
            raise OrderRejectedError("Keine offene Position")
        m = self._market(symbol)
        o = Order(id=f"P{self.acc.next_id}", symbol=symbol,
                  side="sell" if pos.is_long else "buy", kind="market", qty=abs(pos.qty),
                  reduce_only=True, created_ns=now_ns)  # fmt: skip
        self.acc.next_id += 1
        self._execute_market(o, m, now_ns, reason)
        if o.remaining > 0:
            raise OrderRejectedError("Buch zu dünn – Position nur teilweise geschlossen")
        self._changed()

    def close_all(self, now_ns: int) -> list[str]:
        """Kill-Switch: alle Orders löschen, alle Positionen schließen. Fehler je Coin."""
        self.cancel_all()
        errors = []
        for sym in list(self.acc.positions):
            try:
                self.close(sym, now_ns, "Alles schließen")
            except OrderRejectedError as e:
                errors.append(f"{sym}: {e}")
        return errors

    def reset(self) -> None:
        self.acc = Account(wallet=self.cfg.start_balance)
        self._changed()

    # --- Ausführung --------------------------------------------------------------------

    def _execute_market(
        self, o: Order, m: MarketView, now_ns: int, reason: str, limit: Decimal | None = None
    ) -> None:
        """Gegen das Buch der Börse ausführen (bis ``limit``), ein Fill zum Durchschnittspreis."""
        book_side = Side.ASK if o.side == "buy" else Side.BID
        left = o.remaining
        notional = ZERO
        got = ZERO
        for price, qty in m.levels(book_side):
            if left <= 0:
                break
            if limit is not None and ((o.side == "buy" and price > limit) or
                                      (o.side == "sell" and price < limit)):  # fmt: skip
                break
            take = min(left, qty)
            notional += take * price
            got += take
            left -= take
        if got <= 0:
            return
        avg = _round_tick(notional / got, m.tick_size / 100)  # Durchschnitt etwas feiner
        self._fill(o, got, avg, "taker", now_ns, reason, m)

    def _fill(
        self, o: Order, qty: Decimal, price: Decimal, liq: Literal["maker", "taker"],
        now_ns: int, reason: str, m: MarketView,
    ) -> None:  # fmt: skip
        fees = self._fees(m)
        fee = qty * price * (fees.maker if liq == "maker" else fees.taker)
        o.filled += qty
        realized = self._apply(o.symbol, o.side, qty, price, fee, now_ns, reason)
        pos = self.acc.positions.get(o.symbol)
        if pos is not None and (o.tp is not None or o.sl is not None) and not o.reduce_only:
            pos.tp = o.tp if o.tp is not None else pos.tp
            pos.sl = o.sl if o.sl is not None else pos.sl
        self.acc.fills.append(Fill(now_ns, o.id, o.symbol, o.side, qty, price, fee, liq,
                                   realized, reason))  # fmt: skip
        del self.acc.fills[:-_MAX_FILLS]
        if o.remaining <= 0:
            self.acc.orders.pop(o.id, None)
        self._changed()

    def _apply(
        self, symbol: str, side: OrderSide, qty: Decimal, price: Decimal, fee: Decimal,
        now_ns: int, reason: str,
    ) -> Decimal:  # fmt: skip
        """Fill auf die Position buchen. Gibt den realisierten PnL (vor Gebühr) zurück."""
        acc = self.acc
        acc.wallet -= fee
        acc.day_realized -= fee
        signed = qty if side == "buy" else -qty
        pos = acc.positions.get(symbol)
        if pos is None:
            pos = acc.positions[symbol] = Position(symbol, signed, price,
                                                   self.leverage_of(symbol), now_ns)  # fmt: skip
            pos.fees = fee
            pos.max_qty = qty
            return ZERO
        pos.fees += fee
        if (signed > 0) == pos.is_long:  # vergrößern
            new = pos.qty + signed
            pos.entry = (abs(pos.qty) * pos.entry + qty * price) / abs(new)
            pos.qty = new
            pos.max_qty = max(pos.max_qty, abs(new))
            return ZERO
        closing = min(qty, abs(pos.qty))
        pnl = closing * (price - pos.entry) * (1 if pos.is_long else -1)
        acc.wallet += pnl
        acc.day_realized += pnl
        pos.realized += pnl
        pos.exit_notional += closing * price
        pos.exit_qty += closing
        pos.qty += closing if signed > 0 else -closing
        if pos.qty == 0:
            del acc.positions[symbol]
            acc.trades.append(ClosedTrade(
                symbol, "long" if signed < 0 else "short", pos.max_qty, pos.entry,
                pos.exit_notional / pos.exit_qty, pos.realized - pos.fees, pos.fees,
                pos.opened_ns, now_ns, reason,
            ))  # fmt: skip
            del acc.trades[:-_MAX_TRADES]
            # Reduce-Only-Orders ohne Position sind wertlos
            for oid in [i for i, o in acc.orders.items() if o.symbol == symbol and o.reduce_only]:
                del acc.orders[oid]
            rest = qty - closing
            if rest > 0:  # gedreht: Rest eröffnet die Gegenposition
                acc.positions[symbol] = Position(symbol, rest if signed > 0 else -rest, price,
                                                 self.leverage_of(symbol), now_ns,
                                                 max_qty=rest)  # fmt: skip
        return pnl

    # --- Marktdaten --------------------------------------------------------------------

    def on_trade(self, symbol: str, price: Decimal, qty: Decimal, aggressor: Side,
                 now_ns: int) -> None:  # fmt: skip
        """Trade der Börse: arbeitet Warteschlangen ab bzw. füllt durchgehandelte Limits."""
        orders = [o for o in self.acc.orders.values() if o.symbol == symbol and o.kind == "limit"]
        if not orders:
            return
        m = self.markets(symbol)
        if m is None:
            return
        left = qty
        for o in sorted(orders, key=lambda x: x.created_ns):
            assert o.price is not None
            # Kaufs-Limit wird nur von aggressiven Verkäufern getroffen und umgekehrt
            if o.side == "buy" and (aggressor is not Side.ASK or price > o.price):
                continue
            if o.side == "sell" and (aggressor is not Side.BID or price < o.price):
                continue
            if price != o.price:  # durch unseren Preis gehandelt -> sicher dran
                self._fill(o, o.remaining, o.price, "maker", now_ns, "Limit", m)
                continue
            if left <= 0:
                continue
            eat = min(left, o.queue_ahead)
            o.queue_ahead -= eat
            left -= eat
            take = min(left, o.remaining)
            if take > 0:
                left -= take
                self._fill(o, take, o.price, "maker", now_ns, "Limit", m)

    def on_tick(self, now_ns: int) -> None:
        """Alle 100 ms: Buch-basierte Fills, Stops, TP/SL, Liquidation."""
        self._roll_day(now_ns)
        symbols = {o.symbol for o in self.acc.orders.values()} | set(self.acc.positions)
        for sym in symbols:
            m = self.markets(sym)
            bbo = m.bbo() if m is not None else None
            if m is None or bbo is None:
                continue
            bid, ask = bbo
            mid = (bid + ask) / 2
            for o in list(self.acc.orders.values()):
                if o.symbol != sym or o.id not in self.acc.orders:
                    continue
                if o.kind == "limit":
                    assert o.price is not None
                    side = Side.BID if o.side == "buy" else Side.ASK
                    o.queue_ahead = min(o.queue_ahead, m.level_qty(side, o.price))
                    through = ask <= o.price if o.side == "buy" else bid >= o.price
                    if through:
                        self._fill(o, o.remaining, o.price, "maker", now_ns, "Limit", m)
                elif o.kind == "stop":
                    assert o.trigger is not None
                    hit = mid >= o.trigger if o.side == "buy" else mid <= o.trigger
                    if hit:
                        self._trigger(o, m, now_ns)
            pos = self.acc.positions.get(sym)
            if pos is None:
                continue
            liq = pos.liq_price(self.cfg.maintenance_margin)
            if (mid <= liq) if pos.is_long else (mid >= liq):
                self._liquidate(pos, m, now_ns)
                continue
            long = pos.is_long
            if pos.sl is not None and (mid <= pos.sl if long else mid >= pos.sl):
                self._close_quietly(sym, now_ns, "Stop-Loss")
            elif pos.tp is not None and (mid >= pos.tp if long else mid <= pos.tp):
                self._close_quietly(sym, now_ns, "Take-Profit")

    def _trigger(self, o: Order, m: MarketView, now_ns: int) -> None:
        del self.acc.orders[o.id]
        if o.reduce_only:
            pos = self.acc.positions.get(o.symbol)
            if pos is None or (o.side == "buy") == pos.is_long:
                return
            o.qty = o.filled + min(o.remaining, abs(pos.qty))
        self._execute_market(o, m, now_ns, "Stop")
        if o.filled < o.qty:
            self._say("error", f"{o.symbol}: Stop nur teilweise ausgeführt (Buch zu dünn)")
        self._changed()

    def _close_quietly(self, symbol: str, now_ns: int, reason: str) -> None:
        try:
            self.close(symbol, now_ns, reason)
            self._say("ok", f"{symbol}: {reason} ausgeführt")
        except OrderRejectedError as e:
            self._say("error", f"{symbol}: {reason} fehlgeschlagen – {e}")

    def _liquidate(self, pos: Position, m: MarketView, now_ns: int) -> None:
        """Isolierte Margin: Verlust höchstens die Margin (Rest trägt der "Versicherungsfonds")."""
        margin = pos.margin
        wallet_before = self.acc.wallet
        self._close_quietly(pos.symbol, now_ns, "Liquidation")
        loss = wallet_before - self.acc.wallet
        if loss > margin:
            self.acc.wallet += loss - margin
            self.acc.day_realized += loss - margin

    # --- Ausgabe -----------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Zustand für das Frontend (JSON). Zahlen als float – nur Anzeige."""
        acc = self.acc
        positions = []
        upnl = ZERO
        for p in acc.positions.values():
            m = self.markets(p.symbol)
            mark = (self.mid(m) if m is not None else None) or p.entry
            u = p.upnl(mark)
            upnl += u
            positions.append({
                "symbol": p.symbol, "qty": float(p.qty), "entry": float(p.entry),
                "mark": float(mark), "upnl": float(u),
                "roe": float(u / p.margin * 100) if p.margin else 0.0,
                "leverage": p.leverage, "margin": float(p.margin),
                "liq": float(p.liq_price(self.cfg.maintenance_margin)),
                "tp": _f(p.tp), "sl": _f(p.sl), "opened": p.opened_ns // 1_000_000,
                # Break-even inkl. bisheriger Gebühren und Schließ-Gebühr (Taker)
                "breakeven": float(self._breakeven(p)),
            })  # fmt: skip
        return {
            "type": "paper_state",
            "account": {
                "wallet": float(acc.wallet),
                "equity": float(acc.wallet + upnl),
                "upnl": float(upnl),
                "margin": float(self.used_margin()),
                "available": float(self.available()),
                "day_realized": float(acc.day_realized),
                "start": float(self.cfg.start_balance),
                "max_leverage": self.cfg.max_leverage,
                "default_leverage": self.cfg.default_leverage,
                "loss_limit": float(self.cfg.daily_loss_limit_usd),
                "loss_limit_hit": self._loss_limit_hit(),
            },
            "leverage": dict(acc.leverage),
            "positions": positions,
            "orders": [
                {
                    "id": o.id,
                    "symbol": o.symbol,
                    "side": o.side,
                    "kind": o.kind,
                    "qty": float(o.qty),
                    "filled": float(o.filled),
                    "price": _f(o.price),
                    "trigger": _f(o.trigger),
                    "reduce_only": o.reduce_only,
                    "tp": _f(o.tp),
                    "sl": _f(o.sl),
                    "queue": float(o.queue_ahead),
                    "created": o.created_ns // 1_000_000,
                }
                for o in acc.orders.values()
            ],
            "fills": [
                {
                    "ts": f.ts_ns // 1_000_000,
                    "order": f.order_id,
                    "symbol": f.symbol,
                    "side": f.side,
                    "qty": float(f.qty),
                    "price": float(f.price),
                    "fee": float(f.fee),
                    "liq": f.liquidity,
                    "realized": float(f.realized),
                    "reason": f.reason,
                }
                for f in acc.fills[-100:]
            ],
            "trades": [
                {
                    "symbol": t.symbol,
                    "side": t.side,
                    "qty": float(t.qty),
                    "entry": float(t.entry),
                    "exit": float(t.exit),
                    "pnl": float(t.pnl),
                    "fees": float(t.fees),
                    "opened": t.opened_ns // 1_000_000,
                    "closed": t.closed_ns // 1_000_000,
                    "reason": t.reason,
                }
                for t in acc.trades[-200:]
            ],
        }

    def _breakeven(self, p: Position) -> Decimal:
        m = self.markets(p.symbol)
        taker = self._fees(m).taker if m is not None and m.exchange in self.cfg.fees else ZERO
        q = abs(p.qty)
        # (exit - entry) * q * dir - exit * q * taker = fees - realized
        cost = p.fees - p.realized
        if p.is_long:
            return (cost / q + p.entry) / (1 - taker)
        return (p.entry - cost / q) / (1 + taker)


def _f(v: Decimal | None) -> float | None:
    return float(v) if v is not None else None


# --- Speichern ---------------------------------------------------------------------------


def _dec(d: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: (Decimal(v) if k in keys and v is not None else v) for k, v in d.items()}


def account_to_json(acc: Account) -> dict[str, Any]:
    def enc(v: Any) -> Any:
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, dict):
            return {k: enc(x) for k, x in v.items()}
        if isinstance(v, list):
            return [enc(x) for x in v]
        return v

    return {"version": 1, **enc(asdict(acc))}


def account_from_json(d: dict[str, Any]) -> Account:
    pos_keys = ("qty", "entry", "tp", "sl", "realized", "fees", "max_qty", "exit_notional",
                "exit_qty")  # fmt: skip
    ord_keys = ("qty", "price", "trigger", "tp", "sl", "filled", "queue_ahead")
    return Account(
        wallet=Decimal(d["wallet"]),
        day=d.get("day", ""),
        day_realized=Decimal(d.get("day_realized", "0")),
        positions={k: Position(**_dec(v, *pos_keys)) for k, v in d.get("positions", {}).items()},
        orders={k: Order(**_dec(v, *ord_keys)) for k, v in d.get("orders", {}).items()},
        leverage={k: int(v) for k, v in d.get("leverage", {}).items()},
        fills=[Fill(**_dec(f, "qty", "price", "fee", "realized")) for f in d.get("fills", [])],
        trades=[
            ClosedTrade(**_dec(t, "qty", "entry", "exit", "pnl", "fees"))
            for t in d.get("trades", [])
        ],
        next_id=int(d.get("next_id", 1)),
    )
