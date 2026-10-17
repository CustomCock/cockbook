/**
 * Kennzahlen aller Coins (Preis, Funding, Open Interest, Ratios, Liquidationen).
 * Reine Datenhaltung ohne DOM – die Seitenleiste liest daraus.
 */

import type { DailyRow } from "./cycles";
import type { Category, StatFrame, SymbolInfo } from "./net";
import { lastRsi } from "./rsi";
import { coinOf, keyFor, legsOf, venueOf } from "./venue";

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
  /** Tageskerzen (Spot) für ATH, Zyklen, YTD – nur Binance-Keys, siehe `Markets.daily` */
  daily?: DailyRow[];
  sync?: Extract<StatFrame, { t: "sync" }>;
  kind: "crypto" | "tradfi";
  category: Category;
  /** TradFi: Markt der Handelszeiten (Schlüssel in `Markets.sessions`) */
  market: string | null;
  label: string;
}

export type Session = Extract<StatFrame, { t: "sess" }>;

/** Obergrenze der vorgehaltenen Liquidationen (Gesamtmarkt kann sehr aktiv sein). */
const MAX_LIQS = 5000;

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
  /** Handelszeiten der TradFi-Märkte, z. B. "COMMODITY" -> REGULAR/NO_TRADING */
  readonly sessions = new Map<string, Session>();
  version = 0;

  ensure(symbol: string, tickSize?: string): MarketState {
    let m = this.bySymbol.get(symbol);
    if (!m) {
      m = { symbol, tickSize: null, decimals: 2, oiHist: [], kind: "crypto", category: "crypto", market: null, label: symbol.replace(/USDT$/, "") };
      this.bySymbol.set(symbol, m);
    }
    if (tickSize) {
      m.tickSize = Number(tickSize);
      m.decimals = decimalsOf(tickSize);
    }
    return m;
  }

  setInfo(info: SymbolInfo): void {
    const m = this.ensure(info.symbol, info.tick_size);
    if (info.kind) m.kind = info.kind;
    if (info.category) m.category = info.category;
    else if (info.kind === "tradfi") m.category = "tradfi";
    if (info.market) m.market = info.market;
    if (info.label) m.label = info.label;
  }

  apply(f: StatFrame): void {
    this.version++;
    if (f.t === "liq") {
      this.addLiq(f);
      return;
    }
    if (f.t === "sess") {
      this.sessions.set(f.sym, f);
      return;
    }
    const m = this.ensure(f.sym);
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
      case "d1":
        m.daily = f.x;
        break;
      case "sync":
        m.sync = f;
        break;
    }
  }

  private addLiq(f: Extract<StatFrame, { t: "liq" }>): void {
    // Nach Reconnect/Symbolwechsel schickt der Server die letzten Liquidationen erneut
    const key = `${f.sym}|${f.ts}|${f.side}|${f.price}|${f.qty}`;
    if (this.liqKeys.has(key)) return;
    this.liqKeys.add(key);
    const liq: Liq = { ...f, usd: f.price * f.qty };
    // meist neuester -> vorne; nach Reconnect evtl. ältere -> einsortieren
    let i = 0;
    while (i < this.liqs.length && this.liqs[i]!.ts > liq.ts) i++;
    this.liqs.splice(i, 0, liq);
    this.prune(this.liqs[0]!.ts);
  }

  private prune(now: number): void {
    while (
      this.liqs.length &&
      (this.liqs.length > MAX_LIQS || this.liqs[this.liqs.length - 1]!.ts < now - LIQ_WINDOW_MS)
    ) {
      const l = this.liqs.pop()!;
      this.liqKeys.delete(`${l.sym}|${l.ts}|${l.side}|${l.price}|${l.qty}`);
    }
  }

  /** Handelszeit eines TradFi-Symbols (null = Krypto oder unbekannt). */
  session(symbol: string): Session | null {
    const market = this.bySymbol.get(symbol)?.market;
    return market ? (this.sessions.get(market) ?? null) : null;
  }

  /**
   * Key, dessen Kennzahlen für `symbol` gelten: Das kombinierte Buch hat keine eigenen
   * (Ticker, Funding …) – dort zählt Binance als Referenz.
   */
  private ref(symbol: string): MarketState | undefined {
    return this.bySymbol.get(venueOf(symbol) === "multi" ? keyFor("binance", coinOf(symbol)) : symbol);
  }

  /** Tageskerzen (ATH, Zyklen, RSI 1D): Historie gibt es nur von Binance – der Coin ist derselbe. */
  daily(symbol: string): DailyRow[] | undefined {
    return this.bySymbol.get(symbol)?.daily ?? this.bySymbol.get(keyFor("binance", coinOf(symbol)))?.daily;
  }

  /** Orderbuch-Status; kombiniert = synchron nur, wenn alle Börsen synchron sind. */
  sync(symbol: string): Extract<StatFrame, { t: "sync" }> | undefined {
    const legs = legsOf(symbol).map((k) => this.bySymbol.get(k)?.sync);
    if (legs.length === 1) return legs[0];
    if (legs.some((s) => !s)) return undefined;
    const bad = legs.find((s) => !s!.synced);
    return { ...(bad ?? legs[0]!), sym: symbol, resyncs: legs.reduce((n, s) => n + s!.resyncs, 0) };
  }

  /** Letzter Preis: Ticker, sonst Mark-Preis. */
  price(symbol: string): number | null {
    const m = this.ref(symbol);
    return m?.ticker?.last ?? m?.mark?.mp ?? null;
  }

  /**
   * RSI(14) auf Tageskerzen. Die letzte Tageskerze läuft noch: ihr Schlusskurs wird durch
   * den aktuellen Preis ersetzt. Nur die letzten 300 Tage – die Wilder-Glättung hat
   * frühere Kerzen bis dahin praktisch vergessen (Gewicht (13/14)^300 ≈ 2e-10).
   */
  rsi1d(symbol: string, now = Date.now()): number | null {
    const d = this.daily(symbol);
    if (!d || d.length < 2) return null;
    const closes = d.slice(-300).map((r) => r[4]);
    const price = this.price(symbol);
    if (price !== null && now - d[d.length - 1]![0] < 86_400_000) closes[closes.length - 1] = price;
    return lastRsi(closes);
  }

  /** Veränderung 24h in Prozent. */
  change24h(symbol: string): number | null {
    const t = this.ref(symbol)?.ticker;
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
  liqSums(now: number, symbol?: string, windowMs = LIQ_WINDOW_MS): { long: number; short: number } {
    let long = 0;
    let short = 0;
    for (const l of this.liqs) {
      if (l.ts < now - windowMs) break;
      if (symbol && l.sym !== symbol) continue;
      if (l.side === -1) long += l.usd;
      else short += l.usd;
    }
    return { long, short };
  }
}

/** Liquidationen je Sekunde der letzten `seconds` Sekunden (älteste zuerst). */
export function liqPerSecond(
  liqs: Liq[],
  now: number,
  seconds = 60,
): { long: number; short: number }[] {
  const out = Array.from({ length: seconds }, () => ({ long: 0, short: 0 }));
  const end = Math.floor(now / 1000);
  for (const l of liqs) {
    const age = end - Math.floor(l.ts / 1000);
    if (age >= seconds) break;
    if (age < 0) continue;
    const b = out[seconds - 1 - age]!;
    if (l.side === -1) b.long += l.usd;
    else b.short += l.usd;
  }
  return out;
}

/** Coins mit den höchsten Liquidationen im Fenster. */
export function topLiquidated(liqs: Liq[], now: number, windowMs: number, n = 5): { sym: string; usd: number }[] {
  const by = new Map<string, number>();
  for (const l of liqs) {
    if (l.ts < now - windowMs) break;
    by.set(l.sym, (by.get(l.sym) ?? 0) + l.usd);
  }
  return [...by.entries()]
    .map(([sym, usd]) => ({ sym, usd }))
    .sort((a, b) => b.usd - a.usd)
    .slice(0, n);
}
