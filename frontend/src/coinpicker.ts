/**
 * Coin-Auswahl als Dropdown in der Kopfzeile (statt Tabelle in der Seitenleiste – bei
 * 20+ Coins wäre die zu lang). Eigenes Element statt <select>: ein natives Select kann
 * keine Spalten (Preis, 24h, RSI …), keine Farben und keine Suche.
 *
 * Bedienung: Klick oder `/` öffnet, Tippen filtert, ↑/↓ + Enter wählt, Esc schließt.
 */

import { CATEGORY_GROUPS, sessionClass, sessionLong, sessionText } from "./categories";
import { athStats } from "./cycles";
import type { Markets } from "./markets";
import { rsiZone } from "./rsi";

interface Row {
  sym: string;
  el: HTMLButtonElement;
  dot: HTMLElement;
  px: HTMLElement;
  chg: HTMLElement;
  ytd: HTMLElement;
  rsi: HTMLElement;
  sess: HTMLElement;
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function pct(v: number | null): string {
  if (v === null || !Number.isFinite(v)) return "–";
  return `${v > 0 ? "+" : ""}${v.toLocaleString("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} %`;
}

const updown = (v: number | null) => (v === null ? "" : v > 0 ? "up" : v < 0 ? "down" : "");


export class CoinPicker {
  private btn: HTMLButtonElement;
  private btnDot: HTMLElement;
  private btnName: HTMLElement;
  private btnSub: HTMLElement;
  private pop: HTMLElement;
  private search: HTMLInputElement;
  private list: HTMLElement;
  private rows: Row[] = [];
  private groupHeads = new Map<string, HTMLElement>();
  private symbols: string[] = [];
  private active: string | null = null;
  private highlight = -1;

  constructor(
    root: HTMLElement,
    private markets: Markets,
    private onSelect: (symbol: string) => void,
  ) {
    root.classList.add("picker");
    this.btn = el("button", "picker-btn");
    this.btn.type = "button";
    this.btn.title = "Coin wählen (Taste /)";
    this.btnDot = el("span", "dot");
    this.btnName = el("span", "picker-name", "–");
    this.btnSub = el("span", "picker-sub");
    this.btn.append(this.btnDot, this.btnName, this.btnSub, el("span", "picker-caret", "▾"));

    this.pop = el("div", "picker-pop");
    this.pop.hidden = true;
    this.search = el("input", "picker-search");
    this.search.placeholder = "Coin suchen …";
    const head = el("div", "picker-row picker-colhead");
    const rsiHead = el("span", "num", "RSI 1D");
    rsiHead.title = "RSI(14) auf Tageskerzen: ≥ 70 überkauft, ≤ 30 überverkauft";
    const ytdHead = el("span", "num", "Jahr");
    ytdHead.title = "Veränderung seit 1. Januar (UTC)";
    head.append(el("span", "", "Coin"), el("span", "num", "Preis"), el("span", "num", "24h"), ytdHead, rsiHead);
    this.list = el("div", "picker-list");
    const foot = el("div", "picker-foot");
    foot.append(
      el("span", "dot ok"), " Orderbuch läuft  ",
      el("span", "dot"), " startet bei Auswahl (Heatmap ab dann)",
    );
    this.pop.append(this.search, head, this.list, foot);
    root.append(this.btn, this.pop);

    this.btn.addEventListener("click", () => (this.pop.hidden ? this.open() : this.close()));
    this.search.addEventListener("input", () => this.filter());
    this.search.addEventListener("keydown", (e) => this.onKey(e));
    document.addEventListener("pointerdown", (e) => {
      if (!this.pop.hidden && !root.contains(e.target as Node)) this.close();
    });
    window.addEventListener("keydown", (e) => {
      if (e.key === "/" && this.pop.hidden && !(e.target instanceof HTMLInputElement)) {
        e.preventDefault();
        this.open();
      }
    });
  }

  get isOpen(): boolean {
    return !this.pop.hidden;
  }

  setSymbols(symbols: string[]): void {
    if (symbols.length === this.symbols.length && symbols.every((s, i) => s === this.symbols[i])) return;
    this.symbols = [...symbols];
    this.list.replaceChildren();
    this.rows = [];
    this.groupHeads.clear();
    for (const g of CATEGORY_GROUPS) {
      const syms = symbols.filter((s) => (this.markets.bySymbol.get(s)?.category ?? "crypto") === g.id);
      if (!syms.length) continue;
      const gh = el("div", "picker-group", `${g.title} · ${syms.length}`);
      this.groupHeads.set(g.id, gh);
      this.list.append(gh);
      for (const sym of syms) {
        const b = el("button", "picker-row");
        b.type = "button";
        const dot = el("span", "dot");
        const name = el("span", "sym");
        const sess = el("span", "sess");
        name.append(dot, document.createTextNode(this.markets.bySymbol.get(sym)?.label ?? sym), sess);
        const px = el("span", "num");
        const chg = el("span", "num");
        const ytd = el("span", "num");
        const rsi = el("span", "num");
        b.append(name, px, chg, ytd, rsi);
        b.addEventListener("click", () => this.choose(sym));
        this.list.append(b);
        this.rows.push({ sym, el: b, dot, px, chg, ytd, rsi, sess });
      }
    }
    this.filter();
    if (this.active) this.setActive(this.active);
  }

  setActive(symbol: string): void {
    this.active = symbol;
    for (const r of this.rows) r.el.classList.toggle("active", r.sym === symbol);
    const m = this.markets.bySymbol.get(symbol);
    this.btnName.textContent = m?.label ?? symbol;
    const group = CATEGORY_GROUPS.find((g) => g.id === m?.category);
    this.btnSub.textContent = `${symbol} · Binance USDⓈ-M${m?.kind === "tradfi" ? ` · ${(group?.title ?? "TradFi").replace(" (TradFi)", "")}` : ""}`;
  }

  open(): void {
    this.pop.hidden = false;
    this.search.value = "";
    this.filter();
    this.update(Date.now());
    this.search.focus();
  }

  private close(): void {
    this.pop.hidden = true;
  }

  private choose(sym: string): void {
    this.close();
    this.onSelect(sym);
  }

  private visibleRows(): Row[] {
    return this.rows.filter((r) => !r.el.hidden);
  }

  private filter(): void {
    const q = this.search.value.trim().toUpperCase();
    for (const r of this.rows) {
      const label = (this.markets.bySymbol.get(r.sym)?.label ?? "").toUpperCase();
      r.el.hidden = q !== "" && !r.sym.includes(q) && !label.includes(q);
    }
    for (const [cat, gh] of this.groupHeads) {
      gh.hidden = !this.rows.some((r) => !r.el.hidden && this.markets.bySymbol.get(r.sym)?.category === cat);
    }
    this.setHighlight(q ? 0 : -1);
  }

  private setHighlight(i: number): void {
    const vis = this.visibleRows();
    this.highlight = Math.max(-1, Math.min(i, vis.length - 1));
    for (const r of this.rows) r.el.classList.remove("hl");
    const r = vis[this.highlight];
    if (r) {
      r.el.classList.add("hl");
      r.el.scrollIntoView({ block: "nearest" });
    }
  }

  private onKey(e: KeyboardEvent): void {
    if (e.key === "Escape") this.close();
    else if (e.key === "ArrowDown") this.setHighlight(this.highlight + 1);
    else if (e.key === "ArrowUp") this.setHighlight(this.highlight - 1);
    else if (e.key === "Enter") {
      const r = this.visibleRows()[Math.max(0, this.highlight)];
      if (r) this.choose(r.sym);
    } else return;
    e.preventDefault();
  }

  /** Werte aktualisieren (Knopf immer, Liste nur wenn offen). */
  update(now: number): void {
    const mk = this.markets;
    if (this.active) {
      const s = mk.bySymbol.get(this.active)?.sync;
      this.btnDot.className = `dot ${!s ? "" : s.synced ? "ok" : "bad"}`;
    }
    if (this.pop.hidden) return;
    for (const r of this.rows) {
      if (r.el.hidden) continue;
      const m = mk.bySymbol.get(r.sym);
      const price = mk.price(r.sym);
      const dec = m?.decimals ?? 2;
      r.px.textContent =
        price === null ? "–" : price.toLocaleString("de-DE", { minimumFractionDigits: dec, maximumFractionDigits: dec });
      const chg = mk.change24h(r.sym);
      r.chg.textContent = pct(chg);
      r.chg.className = `num ${updown(chg)}`;
      const ytd = m?.daily ? (athStats(m.daily, price, now)?.ytdPct ?? null) : null;
      r.ytd.textContent = pct(ytd);
      r.ytd.className = `num ${updown(ytd)}`;
      const rsi = mk.rsi1d(r.sym, now);
      r.rsi.textContent = rsi === null ? "–" : rsi.toFixed(0);
      r.rsi.className = `num rsi ${rsiZone(rsi)}`;
      const session = mk.session(r.sym);
      r.sess.textContent = session ? sessionText(session) : "";
      r.sess.className = `sess ${sessionClass(session)}`;
      const s = m?.sync;
      r.dot.className = `dot ${!s ? "" : s.synced ? "ok" : "bad"}`;
      const sessTitle = m?.kind === "tradfi" ? ` · Handel: ${sessionLong(session)}` : "";
      r.el.title = (s
        ? `${r.sym}: Orderbuch ${s.synced ? "synchron" : "NICHT synchron"} · Neu-Syncs: ${s.resyncs}` +
          (s.reason ? ` · zuletzt: ${s.reason}` : "")
        : `${r.sym}: Orderbuch startet bei Auswahl`) + sessTitle;
    }
  }
}
