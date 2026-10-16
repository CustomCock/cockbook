/**
 * Kennzahlen aller Coins (Preis, Funding, Open Interest, Ratios, Liquidationen).
 * Reine Datenhaltung ohne DOM – die Seitenleiste liest daraus.
 */

import type { StatFrame } from "./net";

export interface Liq {
  sym: string;
  ts: number;
  side: 1 | -1; // -1 = Long liquidiert, +1 = Short liquidiert
  price: number;
  qty: number;
  usd: number;
}

export interface MarketState {
  symbol: string;
  tickSize: number | null;
  decimals: number;
  mark?: Extract<StatFrame, { t: "mark" }>;
  ticker?: Extract<StatFrame, { t: "tick" }>;
  oi?: Extract<StatFrame, { t: "oi" }>;
  ls?: Extract<StatFrame, { t: "ls" }>;
  tv?: Extract<StatFrame, { t: "tv" }>;
  /** Open-Interest-Verlauf (für Veränderung über Zeitfenster) */
  oiHist: { ts: number; oi: number }[];
}

export const LIQ_WINDOW_MS = 60 * 60 * 1000; // Summen der letzten Stunde
const OI_HIST_MS = 4 * 60 * 60 * 1000;

export function decimalsOf(tickSize: string): number {
  return (tickSize.split(".")[1] ?? "").replace(/0+$/, "").length;
}

export class Markets {
  readonly bySymbol = new Map<string, MarketState>();
  /** Neueste zuerst */
  liqs: Liq[] = [];
  private liqKeys = new Set<string>();
  version = 0;

  ensure(symbol: string, tickSize?: string): MarketState {
    let m = this.bySymbol.get(symbol);
    if (!m) {
      m = { symbol, tickSize: null, decimals: 2, oiHist: [] };
      this.bySymbol.set(symbol, m);
    }
    if (tickSize) {
      m.tickSize = Number(tickSize);
      m.decimals = decimalsOf(tickSize);
    }
    return m;
  }

  apply(f: StatFrame): void {
    const m = this.ensure(f.sym);
    this.version++;
    switch (f.t) {
      case "mark":
        m.mark = f;
        break;
      case "tick":
        m.ticker = f;
        break;
      case "oi": {
        m.oi = f;
        const last = m.oiHist[m.oiHist.length - 1];
        if (!last || f.ts > last.ts) m.oiHist.push({ ts: f.ts, oi: f.oi });
        while (m.oiHist.length && m.oiHist[0]!.ts < f.ts - OI_HIST_MS) m.oiHist.shift();
        break;
      }
      case "ls":
        m.ls = f;
        break;
      case "tv":
        m.tv = f;
        break;
      case "liq": {
        // Nach Reconnect/Symbolwechsel schickt der Server die letzten Liquidationen erneut
        const key = `${f.sym}|${f.ts}|${f.side}|${f.price}|${f.qty}`;
        if (this.liqKeys.has(key)) return;
        this.liqKeys.add(key);
        this.liqs.unshift({ ...f, usd: f.price * f.qty });
        this.prune(f.ts);
        break;
      }
    }
  }

  private prune(now: number): void {
    while (this.liqs.length && this.liqs[this.liqs.length - 1]!.ts < now - LIQ_WINDOW_MS) {
      const l = this.liqs.pop()!;
      this.liqKeys.delete(`${l.sym}|${l.ts}|${l.side}|${l.price}|${l.qty}`);
    }
  }

  /** Letzter Preis: Ticker, sonst Mark-Preis. */
  price(symbol: string): number | null {
    const m = this.bySymbol.get(symbol);
    return m?.ticker?.last ?? m?.mark?.mp ?? null;
  }

  /** Veränderung 24h in Prozent. */
  change24h(symbol: string): number | null {
    const t = this.bySymbol.get(symbol)?.ticker;
    return t && t.open ? ((t.last - t.open) / t.open) * 100 : null;
  }

  /** Open-Interest-Veränderung in Prozent über `minutes` (null, wenn zu wenig Verlauf). */
  oiChange(symbol: string, minutes: number): number | null {
    const h = this.bySymbol.get(symbol)?.oiHist;
    if (!h || h.length < 2) return null;
    const last = h[h.length - 1]!;
    const target = last.ts - minutes * 60_000;
    if (h[0]!.ts > target) return null;
    let ref = h[0]!;
    for (const p of h) {
      if (p.ts > target) break;
      ref = p;
    }
    return ref.oi ? ((last.oi - ref.oi) / ref.oi) * 100 : null;
  }

  /** Liquidations-Summen (USDT) im Zeitfenster, optional nur für ein Symbol. */
  liqSums(now: number, symbol?: string): { long: number; short: number } {
    let long = 0;
    let short = 0;
    for (const l of this.liqs) {
      if (l.ts < now - LIQ_WINDOW_MS) break;
      if (symbol && l.sym !== symbol) continue;
      if (l.side === -1) long += l.usd;
      else short += l.usd;
    }
    return { long, short };
  }
}
