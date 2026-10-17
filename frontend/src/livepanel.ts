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

/** Linien des echten Kontos. `editable`: Orders und TP/SL ziehen bzw. per × löschen. */
export function liveLines(symbol: string, s: LiveState | null, editable = false): TradeLine[] {
  const out: TradeLine[] = [];
  if (!s) return out;
  for (const p of s.positions ?? []) {
    if (p.symbol !== symbol || p.entry === null) continue;
    const long = p.qty > 0;
    const q = Math.abs(p.qty).toLocaleString("de-DE", { maximumFractionDigits: 4 });
    out.push({ price: p.entry, kind: "entry", label: `LIVE ${long ? "Long" : "Short"} ${q} · ${usd(p.upnl, true)}`, color: long ? "#55e5a8" : "#ff7385" });
    if (p.liq !== null) out.push({ price: p.liq, kind: "liq", label: "Liquidation", color: "#ffa833" });
    for (const k of ["tp", "sl"] as const) {
      const price = p[k];
      if (price === null) continue;
      const target = { type: k, symbol } as const;
      out.push({
        price,
        kind: k,
        label: `LIVE ${k.toUpperCase()} ${usd((price - p.entry) * p.qty, true)}`,
        color: k === "tp" ? "#55e5a8" : "#ff7385",
        ...(editable ? { drag: target, cancel: target } : {}),
      });
    }
  }
  for (const o of s.orders ?? []) {
    if (o.symbol !== symbol || o.price === null) continue;
    const q = (o.qty - o.filled).toLocaleString("de-DE", { maximumFractionDigits: 4 });
    out.push({
      price: o.price,
      kind: "order",
      label: `LIVE Limit ${o.side === "buy" ? "Kauf" : "Verkauf"} ${q}${o.reduce_only ? " RO" : ""}`,
      color: o.side === "buy" ? "#5accff" : "#ff9de0",
      ...(editable
        ? { drag: { type: "order" as const, id: o.id, field: "price" as const }, cancel: { type: "order" as const, id: o.id } }
        : {}),
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

/** Was das Panel von außen braucht (Freigabe, Bestätigung, Aktionen). */
export interface LiveControls {
  unlocked(): boolean;
  setUnlocked(on: boolean): void;
  confirmOrders(): boolean;
  setConfirmOrders(on: boolean): void;
  /** Anfrage ans echte Konto (inkl. Bestätigungsdialog, wo nötig) */
  act(op: string, payload: Record<string, unknown>, confirmText?: string): void;
}

export class LivePanel {
  /** oben: Status, Freigabe, Konto – unten: Positionen, Orders, Kill-Switch (Ordermaske dazwischen) */
  readonly root: HTMLElement;
  readonly bottom: HTMLElement;
  state: LiveState | null = null;

  constructor(
    private markets: Markets,
    private onSelectSymbol: (symbol: string) => void,
    private ctl: LiveControls,
  ) {
    this.root = el("section", "live");
    this.bottom = el("section", "live live-bottom");
  }

  /** Darf hier gehandelt werden? (Config erlaubt es, verbunden, im Browser freigeschaltet) */
  get canTrade(): boolean {
    const s = this.state;
    return !!s && s.status === "ok" && !s.read_only && this.ctl.unlocked();
  }

  render(currentSymbol: string | null, dec: number): void {
    // Nicht neu bauen, während in einem Feld getippt wird (Zustand kommt jede Sekunde)
    const typing = document.activeElement instanceof HTMLInputElement;
    if (typing && (this.root.contains(document.activeElement) || this.bottom.contains(document.activeElement))) return;
    const s = this.state;
    const top = this.root;
    let r = top;
    top.replaceChildren();
    this.bottom.replaceChildren();
    const tradeOn = !!s && !s.read_only;
    r.append(el("div", "live-banner", tradeOn ? "ECHTES KONTO · ECHTES GELD" : "ECHTES KONTO · nur lesen"));
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
    // Freigabe
    if (s.read_only) {
      r.append(el("div", "live-help", "Nur lesen. Zum Handeln in config.yaml trading.live.trading_enabled: true setzen und das Backend neu starten."));
    } else {
      const box = el("div", "live-arm");
      if (this.ctl.unlocked()) {
        const lock = el("button", "trade-small", "Live-Trading sperren");
        lock.type = "button";
        lock.addEventListener("click", () => {
          this.ctl.setUnlocked(false);
          this.render(currentSymbol, dec);
        });
        const lab = el("label", "toggle");
        const cb = el("input");
        cb.type = "checkbox";
        cb.checked = this.ctl.confirmOrders();
        cb.addEventListener("change", () => this.ctl.setConfirmOrders(cb.checked));
        lab.append(cb, el("span", "", "Jede Order bestätigen"));
        box.append(el("span", "live-armed", "● Live-Trading frei"), lock, lab);
      } else {
        const unlock = el("button", "live-unlock", "Live-Trading freischalten");
        unlock.type = "button";
        unlock.addEventListener("click", () => {
          if (confirm("Live-Trading freischalten?\n\nOrders gehen ab jetzt mit ECHTEM GELD an Bitunix. Gilt bis du sperrst oder den Tab schließt.")) {
            this.ctl.setUnlocked(true);
            this.render(currentSymbol, dec);
          }
        });
        box.append(unlock, el("div", "trade-empty", "Ordermaske, Klick-Trading und Linien sind gesperrt, bis du freischaltest."));
      }
      r.append(box);
    }
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
    const can = this.canTrade;
    r = this.bottom;
    const f = (v: number | null) => (v === null ? "–" : v.toLocaleString("de-DE", { minimumFractionDigits: dec, maximumFractionDigits: dec }));
    const positions = s.positions ?? [];
    r.append(el("h4", "", `Positionen (${positions.length})`));
    if (!positions.length) r.append(el("div", "trade-empty", "keine"));
    for (const p of positions) {
      const long = p.qty > 0;
      const here = p.symbol === currentSymbol;
      const card = el("div", `trade-card ${long ? "long" : "short"}${here ? "" : " other"}`);
      const name = this.markets.bySymbol.get(p.symbol)?.label ?? coinOf(p.symbol);
      const head = el("div", "trade-card-h", `${name} ${long ? "Long" : "Short"} ${Math.abs(p.qty).toLocaleString("de-DE", { maximumFractionDigits: 6 })} · ${p.leverage}x ${p.margin_mode === "ISOLATION" ? "isoliert" : (p.margin_mode ?? "")}`);
      if (!here) {
        head.title = "Klick: Chart dieses Coins öffnen";
        head.style.cursor = "pointer";
        head.addEventListener("click", () => this.onSelectSymbol(p.symbol));
      }
      card.append(
        head,
        this.kv("Einstieg", f(p.entry)),
        this.kv("Mark", f(p.mark)),
        this.kv("uPnL", `${usd(p.upnl, true)} (${p.roe.toFixed(1)} %)`, cls(p.upnl)),
        this.kv("Liquidation", f(p.liq)),
        this.kv("Gebühren / Funding", `${usd(-p.fee)} / ${usd(p.funding, true)}`),
      );
      const tp = el("input", "trade-input");
      const sl = el("input", "trade-input");
      tp.placeholder = "TP";
      sl.placeholder = "SL";
      if (p.tp !== null) tp.value = String(p.tp);
      if (p.sl !== null) sl.value = String(p.sl);
      const set = el("button", "trade-small", "TP/SL setzen");
      set.type = "button";
      set.disabled = !can;
      set.addEventListener("click", () =>
        this.ctl.act("tpsl", { symbol: p.symbol, tp: tp.value.trim() || null, sl: sl.value.trim() || null }),
      );
      const row = el("div", "trade-tpsl");
      row.append(tp, sl, set);
      const close = el("button", "trade-small danger", "Schließen (Market)");
      close.type = "button";
      close.addEventListener("click", () =>
        this.ctl.act("close", { symbol: p.symbol }, `${name}-Position (${long ? "Long" : "Short"} ${Math.abs(p.qty)}) zum Marktpreis SCHLIESSEN?`),
      );
      card.append(row, close);
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
      const x = el("button", "trade-x", "×");
      x.type = "button";
      x.title = "Order bei Bitunix löschen";
      x.addEventListener("click", () => this.ctl.act("cancel", { symbol: o.symbol, id: o.id }));
      row.append(x);
      r.append(row);
    }
    if (tradeOn) {
      const kill = el("button", "trade-kill", "LIVE: Alles schließen (Kill-Switch)");
      kill.type = "button";
      kill.title = "Alle Orders löschen und alle Positionen bei Bitunix zum Marktpreis schließen";
      kill.addEventListener("click", () =>
        this.ctl.act("close_all", {}, "KILL-SWITCH: Alle Bitunix-Orders löschen und ALLE Positionen zum Marktpreis schließen?"),
      );
      r.append(kill);
    }
  }

  private kv(k: string, v: string, c = ""): HTMLElement {
    const d = el("div", "kv");
    d.append(el("span", "k", k), el("span", `v ${c}`, v));
    return d;
  }
}
