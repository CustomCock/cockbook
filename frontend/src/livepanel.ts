/**
 * Echtes Bitunix-Konto im Handels-Panel – Stufe 1: nur anzeigen.
 *
 * Alles hier kommt vom Backend, das per signierter API liest. Der Browser sieht weder Key
 * noch Secret (nur die letzten 4 Zeichen des Keys zur Kontrolle).
 */

import type { LiveState } from "./net";
import type { Markets } from "./markets";
import type { TradeLine } from "./paper";
import { coinOf } from "./venue";

const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

const usd = (v: number, sign = false) =>
  `${sign && v > 0 ? "+" : ""}${v.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} $`;
const cls = (v: number) => (v > 0 ? "up" : v < 0 ? "down" : "");

/** Linien des echten Kontos (Stufe 1: ohne Ziehen/Löschen). */
export function liveLines(symbol: string, s: LiveState | null): TradeLine[] {
  const out: TradeLine[] = [];
  if (!s) return out;
  for (const p of s.positions ?? []) {
    if (p.symbol !== symbol || p.entry === null) continue;
    const long = p.qty > 0;
    const q = Math.abs(p.qty).toLocaleString("de-DE", { maximumFractionDigits: 4 });
    out.push({ price: p.entry, kind: "entry", label: `LIVE ${long ? "Long" : "Short"} ${q} · ${usd(p.upnl, true)}`, color: long ? "#55e5a8" : "#ff7385" });
    if (p.liq !== null) out.push({ price: p.liq, kind: "liq", label: "Liquidation", color: "#ffa833" });
    if (p.tp !== null) out.push({ price: p.tp, kind: "tp", label: `TP ${usd((p.tp - p.entry) * p.qty, true)}`, color: "#55e5a8" });
    if (p.sl !== null) out.push({ price: p.sl, kind: "sl", label: `SL ${usd((p.sl - p.entry) * p.qty, true)}`, color: "#ff7385" });
  }
  for (const o of s.orders ?? []) {
    if (o.symbol !== symbol || o.price === null) continue;
    const q = (o.qty - o.filled).toLocaleString("de-DE", { maximumFractionDigits: 4 });
    out.push({
      price: o.price,
      kind: "order",
      label: `LIVE Limit ${o.side === "buy" ? "Kauf" : "Verkauf"} ${q}${o.reduce_only ? " RO" : ""}`,
      color: o.side === "buy" ? "#5accff" : "#ff9de0",
    });
  }
  return out;
}

const STATUS_TEXT: Record<string, string> = {
  no_keys: "Keine API-Keys gesetzt",
  off: "Ausgeschaltet (config.yaml: trading.live.enabled)",
  connecting: "verbinde …",
  ok: "verbunden",
  error: "Fehler",
};

export class LivePanel {
  readonly root: HTMLElement;
  state: LiveState | null = null;

  constructor(
    private markets: Markets,
    private onSelectSymbol: (symbol: string) => void,
  ) {
    this.root = el("section", "live");
  }

  render(currentSymbol: string | null, dec: number): void {
    const s = this.state;
    const r = this.root;
    r.replaceChildren();
    r.append(el("div", "live-banner", "ECHTES KONTO · Stufe 1: nur lesen – Orders folgen in Stufe 2"));
    if (!s) {
      r.append(el("div", "trade-empty", "Warte auf das Backend …"));
      return;
    }
    const st = el("div", `live-status ${s.status}`);
    const dot = el("span", `dot ${s.status === "ok" ? (s.ws ? "ok" : "") : s.status === "error" ? "bad" : ""}`);
    st.append(dot, ` ${STATUS_TEXT[s.status] ?? s.status}`);
    if (s.key) st.append(el("span", "live-key", ` · Key ${s.key}`));
    if (s.status === "ok") {
      st.append(el("span", "live-key", ` · ${s.ws ? "Live-Updates" : "nur REST"} · Stand ${new Date(s.updated).toLocaleTimeString("de-DE")}`));
    }
    r.append(st);
    if (s.message) r.append(el("div", "trade-warn", s.message));
    if (s.status === "no_keys") {
      const help = el("div", "live-help");
      help.innerHTML =
        "So verbindest du dein Bitunix-Konto:<br>1. Bei Bitunix einen API-Key nur mit <b>Futures-Trading</b> anlegen " +
        "(kein Abheben/Transfer), IP-Whitelist = deine IP.<br>2. Im Terminal, in dem du das Backend startest:<br>" +
        "<code>export BITUNIX_API_KEY=…</code><br><code>export BITUNIX_API_SECRET=…</code><br>" +
        "(Windows PowerShell: <code>$env:BITUNIX_API_KEY=\"…\"</code>)<br>3. Backend neu starten.<br>" +
        "Keys nie in eine Datei im Repo schreiben.";
      r.append(help);
      return;
    }
    for (const w of s.warnings ?? []) r.append(el("div", "trade-warn", w));
    const a = s.account;
    if (a) {
      const acct = el("div", "trade-acct");
      acct.append(
        this.kv("Equity ≈", usd(a.equity)),
        this.kv("Verfügbar", usd(a.available)),
        this.kv("uPnL", usd(a.upnl, true), cls(a.upnl)),
        this.kv("In Margin", usd(a.margin + a.frozen)),
      );
      r.append(acct);
    }
    const positions = s.positions ?? [];
    r.append(el("h4", "", `Positionen (${positions.length})`));
    if (!positions.length) r.append(el("div", "trade-empty", "keine"));
    const f = (v: number | null) => (v === null ? "–" : v.toLocaleString("de-DE", { minimumFractionDigits: dec, maximumFractionDigits: dec }));
    for (const p of positions) {
      const long = p.qty > 0;
      const here = p.symbol === currentSymbol;
      const card = el("div", `trade-card ${long ? "long" : "short"}${here ? "" : " other"}`);
      const name = this.markets.bySymbol.get(p.symbol)?.label ?? coinOf(p.symbol);
      card.append(
        el("div", "trade-card-h", `${name} ${long ? "Long" : "Short"} ${Math.abs(p.qty).toLocaleString("de-DE", { maximumFractionDigits: 6 })} · ${p.leverage}x ${p.margin_mode === "ISOLATION" ? "isoliert" : (p.margin_mode ?? "")}`),
        this.kv("Einstieg", f(p.entry)),
        this.kv("Mark", f(p.mark)),
        this.kv("uPnL", `${usd(p.upnl, true)} (${p.roe.toFixed(1)} %)`, cls(p.upnl)),
        this.kv("Liquidation", f(p.liq)),
        this.kv("TP / SL", `${f(p.tp)} / ${f(p.sl)}`),
        this.kv("Gebühren / Funding", `${usd(-p.fee)} / ${usd(p.funding, true)}`),
      );
      if (!here) {
        card.title = "Klick: Chart dieses Coins öffnen";
        card.addEventListener("click", () => this.onSelectSymbol(p.symbol));
      }
      r.append(card);
    }
    const orders = s.orders ?? [];
    r.append(el("h4", "", `Offene Orders (${orders.length})`));
    if (!orders.length) r.append(el("div", "trade-empty", "keine"));
    for (const o of orders) {
      const row = el("div", `trade-order ${o.side}`);
      const name = this.markets.bySymbol.get(o.symbol)?.label ?? coinOf(o.symbol);
      const q = (o.qty - o.filled).toLocaleString("de-DE", { maximumFractionDigits: 6 });
      row.append(el("span", "", `${name} ${o.kind === "limit" ? "Limit" : "Market"} ${o.side === "buy" ? "Kauf" : "Verkauf"} ${q} @ ${f(o.price)}${o.reduce_only ? " · RO" : ""}`));
      r.append(row);
    }
  }

  private kv(k: string, v: string, c = ""): HTMLElement {
    const d = el("div", "kv");
    d.append(el("span", "k", k), el("span", `v ${c}`, v));
    return d;
  }
}
