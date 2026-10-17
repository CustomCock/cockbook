/**
 * Paper-Trading im Frontend: reine Rechenhilfen (getestet) für Ordermaske, Klick-Trading
 * und die Linien im Chart. Ausgeführt wird im Backend – hier wird nur vorgeschlagen,
 * angezeigt und umgerechnet.
 */

import type { PaperOrder, PaperPosition } from "./net";

export type OrderSide = "buy" | "sell";
export type OrderKind = "market" | "limit" | "stop";

/** Auf das Tick-Raster runden (gegen float-Reste wie 81234.30000001). */
export function roundTick(price: number, tick: number): number {
  const decimals = Math.max(0, Math.round(-Math.log10(tick)) + 2);
  return Number((Math.round(price / tick) * tick).toFixed(decimals));
}

/** Menge auf die Schrittweite abrunden (nie mehr als gewollt). */
export function floorStep(qty: number, step: number): number {
  const decimals = Math.max(0, Math.round(-Math.log10(step)) + 2);
  return Number((Math.floor(qty / step + 1e-9) * step).toFixed(decimals));
}

/**
 * Klick-Trading wie bei DOM-Tradern üblich:
 * - Bid-Spalte = kaufen. Unter dem Ask: Limit (wartet), auf/über dem Ask: Stop (Ausbruch).
 * - Ask-Spalte = verkaufen. Über dem Bid: Limit, auf/unter dem Bid: Stop.
 * - Im Chart (Strg+Klick): unter dem Mid Kauf-Limit, darüber Verkaufs-Limit.
 */
export function clickOrder(
  where: "bid" | "ask" | "chart",
  price: number,
  bid: number,
  ask: number,
): { side: OrderSide; kind: OrderKind } {
  if (where === "chart") return { side: price < (bid + ask) / 2 ? "buy" : "sell", kind: "limit" };
  if (where === "bid") return { side: "buy", kind: price < ask ? "limit" : "stop" };
  return { side: "sell", kind: price > bid ? "limit" : "stop" };
}

/** Eingabe in Coin oder USDT -> Coin-Menge (abgerundet). */
export function qtyFromInput(amount: number, unit: "coin" | "usdt", price: number, step: number): number {
  if (!(amount > 0) || !(price > 0)) return 0;
  return floorStep(unit === "coin" ? amount : amount / price, step);
}

/** Menge für `pct` % der freien Margin bei `leverage` (Gebühr eingerechnet). */
export function qtyForPercent(
  pct: number,
  available: number,
  leverage: number,
  price: number,
  step: number,
  takerFee = 0.0006,
): number {
  if (!(available > 0) || !(price > 0)) return 0;
  const notional = (available * (pct / 100) * leverage) / (1 + takerFee * leverage);
  return floorStep(notional / price, step);
}

export interface BracketInfo {
  tpPnl: number | null;
  slPnl: number | null;
  /** Chance/Risiko (TP-Gewinn / SL-Verlust) */
  rr: number | null;
  tpPct: number | null;
  slPct: number | null;
}

/** Was TP/SL bei dieser Order bringen bzw. kosten (ohne Gebühren). */
export function bracketInfo(side: OrderSide, entry: number, qty: number, tp: number | null, sl: number | null): BracketInfo {
  const dir = side === "buy" ? 1 : -1;
  const tpPnl = tp !== null && entry > 0 ? (tp - entry) * qty * dir : null;
  const slPnl = sl !== null && entry > 0 ? (sl - entry) * qty * dir : null;
  return {
    tpPnl,
    slPnl,
    rr: tpPnl !== null && slPnl !== null && slPnl < 0 ? tpPnl / -slPnl : null,
    tpPct: tp !== null && entry > 0 ? ((tp - entry) / entry) * 100 * dir : null,
    slPct: sl !== null && entry > 0 ? ((sl - entry) / entry) * 100 * dir : null,
  };
}

/** Eine waagrechte Linie im Chart (Position, Order, TP/SL, Liquidation). */
export interface TradeLine {
  price: number;
  kind: "entry" | "breakeven" | "tp" | "sl" | "liq" | "order";
  label: string;
  color: string;
  /** Ziehen ändert: Order-Preis/Trigger bzw. TP/SL der Position */
  drag?: { type: "order"; id: string; field: "price" | "trigger" } | { type: "tp" | "sl"; symbol: string };
  /** × löscht die Order bzw. entfernt TP/SL */
  cancel?: { type: "order"; id: string } | { type: "tp" | "sl"; symbol: string };
}

const COL = {
  long: "#55e5a8",
  short: "#ff7385",
  be: "#c9ccd1",
  tp: "#55e5a8",
  sl: "#ff7385",
  liq: "#ffa833",
  buy: "#5accff",
  sell: "#ff9de0",
};

function qtyText(q: number): string {
  return Math.abs(q).toLocaleString("de-DE", { maximumFractionDigits: 4 });
}

function money(v: number): string {
  return `${v >= 0 ? "+" : ""}${v.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} $`;
}

/** Linien für ein Symbol aus dem Paper-Zustand. */
export function tradeLines(symbol: string, positions: PaperPosition[], orders: PaperOrder[]): TradeLine[] {
  const out: TradeLine[] = [];
  const pos = positions.find((p) => p.symbol === symbol);
  if (pos) {
    const long = pos.qty > 0;
    out.push({
      price: pos.entry,
      kind: "entry",
      label: `${long ? "Long" : "Short"} ${qtyText(pos.qty)} · ${money(pos.upnl)} (${pos.roe.toFixed(1)} %)`,
      color: long ? COL.long : COL.short,
    });
    out.push({ price: pos.breakeven, kind: "breakeven", label: "Break-even", color: COL.be });
    out.push({ price: pos.liq, kind: "liq", label: "Liquidation", color: COL.liq });
    for (const k of ["tp", "sl"] as const) {
      const price = pos[k];
      if (price === null) continue;
      const pnl = (price - pos.entry) * pos.qty;
      out.push({
        price,
        kind: k,
        label: `${k === "tp" ? "TP" : "SL"} ${money(pnl)}`,
        color: COL[k],
        drag: { type: k, symbol },
        cancel: { type: k, symbol },
      });
    }
  }
  for (const o of orders) {
    if (o.symbol !== symbol) continue;
    const price = o.kind === "stop" ? o.trigger : o.price;
    if (price === null) continue;
    const kind = o.kind === "stop" ? "Stop" : "Limit";
    const queue = o.kind === "limit" && o.queue > 0 ? ` · ${qtyText(o.queue)} vor dir` : "";
    out.push({
      price,
      kind: "order",
      label: `${kind} ${o.side === "buy" ? "Kauf" : "Verkauf"} ${qtyText(o.qty - o.filled)}${o.reduce_only ? " RO" : ""}${queue}`,
      color: o.side === "buy" ? COL.buy : COL.sell,
      drag: { type: "order", id: o.id, field: o.kind === "stop" ? "trigger" : "price" },
      cancel: { type: "order", id: o.id },
    });
  }
  return out;
}
