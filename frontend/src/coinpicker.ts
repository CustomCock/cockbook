/**
 * Coin-Auswahl: Knopf in der Kopfzeile öffnet ein Menü mit
 *
 *   [Krypto 20] [Aktien 14] [ETFs 6] [Rohstoffe 8] …      [Suche]
 *   Coin            Preis     24h     Jahr   RSI 1D   (Spaltenkopf klickbar = sortieren)
 *   BTC          65.003,7   +2,0 %  +56,7 %     56
 *   …
 *
 * Die Kategorie-Leiste wählt, was die Liste zeigt; ein Klick auf eine Zeile öffnet den
 * Coin. Die Suche ist optional: Sobald man tippt, durchsucht sie alle Kategorien.
 * Tastatur: ↑/↓ wählen, Enter öffnen, ←/→ Kategorie wechseln, Esc schließen, `/` öffnen.
 *
 * Eigenes Element statt <select>: ein natives Select kann keine Spalten, Farben,
 * Kategorien-Leiste oder Suche.
 */

import { CATEGORY_GROUPS, sessionClass, sessionLong, sessionText } from "./categories";
import { athStats } from "./cycles";
import type { Markets } from "./markets";
import type { Category } from "./net";
import { rsiZone } from "./rsi";

const CAT_KEY = "cockbook.picker.v1";

type SortKey = "default" | "name" | "price" | "chg" | "ytd" | "rsi";

interface Row {
  sym: string;
  el: HTMLButtonElement;
  dot: HTMLElement;
  name: HTMLElement;
  sess: HTMLElement;
  cat: HTMLElement;
  px: HTMLElement;
  chg: HTMLElement;
  ytd: HTMLElement;
  rsi: HTMLElement;
  /** Sortierwerte (bei jedem Update neu) */
  v: Record<Exclude<SortKey, "default" | "name">, number | null>;
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

const shortTitle = (c: Category) =>
  (CATEGORY_GROUPS.find((g) => g.id === c)?.title ?? c).replace(" (TradFi)", "");

export class CoinPicker {
  private btn: HTMLButtonElement;
  private btnDot: HTMLElement;
  private btnName: HTMLElement;
  private btnSub: HTMLElement;
  private pop: HTMLElement;
  private catBar: HTMLElement;
  private catButtons = new Map<Category, HTMLButtonElement>();
  private search: HTMLInputElement;
  private list: HTMLElement;
  private empty: HTMLElement;
  private sessLine: HTMLElement;
  private heads = new Map<SortKey, HTMLElement>();
  private rows = new Map<string, Row>();
  private symbols: string[] = [];
  private active: string | null = null;
  private category: Category = "crypto";
  private sort: { key: SortKey; dir: 1 | -1 } = { key: "default", dir: 1 };
  /** aktuell sichtbare Zeilen in Anzeigereihenfolge */
  private visible: Row[] = [];
  private highlight = -1;

  constructor(
    root: HTMLElement,
    private markets: Markets,
    private onSelect: (symbol: string) => void,
  ) {
    root.classList.add("picker");
    try {
      const c = localStorage.getItem(CAT_KEY);
      if (c && CATEGORY_GROUPS.some((g) => g.id === c)) this.category = c as Category;
    } catch {
      /* egal */
    }

    this.btn = el("button", "picker-btn");
    this.btn.type = "button";
    this.btn.title = "Coin wählen (Taste /)";
    this.btnDot = el("span", "dot");
    this.btnName = el("span", "picker-name", "–");
    this.btnSub = el("span", "picker-sub");
    this.btn.append(this.btnDot, this.btnName, this.btnSub, el("span", "picker-caret", "▾"));

    this.pop = el("div", "picker-pop");
    this.pop.hidden = true;

    // Kopf: Kategorien + Suche
    const top = el("div", "picker-top");
    this.catBar = el("div", "picker-cats");
    this.catBar.setAttribute("role", "tablist");
    this.search = el("input", "picker-search");
    this.search.type = "search";
    this.search.placeholder = "Suchen …";
    top.append(this.catBar, this.search);
    this.sessLine = el("div", "picker-sessline");

    // Spaltenköpfe (Klick = sortieren)
    const head = el("div", "picker-row picker-colhead");
    const cols: [SortKey, string, string, string][] = [
      ["name", "Coin", "", "Nach Name sortieren"],
      ["price", "Preis", "num", "Nach Preis sortieren"],
      ["chg", "24h", "num", "Veränderung 24 h"],
      ["ytd", "Jahr", "num", "Veränderung seit 1. Januar (UTC)"],
      ["rsi", "RSI 1D", "num", "RSI(14) auf Tageskerzen: ≥ 70 überkauft, ≤ 30 überverkauft"],
    ];
    for (const [key, label, cls, title] of cols) {
      const h = el("span", `sortable ${cls}`, label);
      h.title = `${title} – Klick sortiert`;
      h.addEventListener("click", () => this.toggleSort(key));
      this.heads.set(key, h);
      head.append(h);
    }

    this.list = el("div", "picker-list");
    this.empty = el("div", "picker-empty", "Keine Treffer");
    this.empty.hidden = true;
    const foot = el("div", "picker-foot");
    foot.append(
      el("span", "dot ok"),
      " Orderbuch läuft   ",
      el("span", "dot"),
      " startet bei Auswahl   ·   ↑↓ Enter  ·  ←→ Kategorie  ·  Esc",
    );
    this.pop.append(top, this.sessLine, head, this.list, this.empty, foot);
    root.append(this.btn, this.pop);

    this.btn.addEventListener("click", () => (this.pop.hidden ? this.open() : this.close()));
    this.search.addEventListener("input", () => this.refresh(true));
    document.addEventListener("pointerdown", (e) => {
      if (!this.pop.hidden && !root.contains(e.target as Node)) this.close();
    });
    // Tastatur, solange das Menü offen ist (auch ohne Fokus im Suchfeld)
    window.addEventListener(
      "keydown",
      (e) => {
        if (this.pop.hidden) {
          if (e.key === "/" && !(e.target instanceof HTMLInputElement)) {
            e.preventDefault();
            this.open();
          }
          return;
        }
        this.onKey(e);
      },
      true,
    );
    this.list.addEventListener("mousemove", (e) => {
      const rowEl = (e.target as HTMLElement).closest(".picker-row") as HTMLElement | null;
      const i = rowEl ? this.visible.findIndex((r) => r.el === rowEl) : -1;
      if (i >= 0 && i !== this.highlight) this.setHighlight(i, false);
    });
  }

  get isOpen(): boolean {
    return !this.pop.hidden;
  }

  setSymbols(symbols: string[]): void {
    if (symbols.length === this.symbols.length && symbols.every((s, i) => s === this.symbols[i])) return;
    this.symbols = [...symbols];
    for (const sym of symbols) if (!this.rows.has(sym)) this.rows.set(sym, this.createRow(sym));
    for (const sym of [...this.rows.keys()]) {
      if (!symbols.includes(sym)) {
        this.rows.get(sym)!.el.remove();
        this.rows.delete(sym);
      }
    }
    this.buildCategoryBar();
    if (!this.pop.hidden) this.refresh(false);
    if (this.active) this.setActive(this.active);
  }

  setActive(symbol: string): void {
    this.active = symbol;
    for (const r of this.rows.values()) r.el.classList.toggle("active", r.sym === symbol);
    const m = this.markets.bySymbol.get(symbol);
    this.btnName.textContent = m?.label ?? symbol;
    this.btnSub.textContent = `${symbol} · Binance USDⓈ-M${m?.kind === "tradfi" ? ` · ${shortTitle(m.category)}` : ""}`;
  }

  open(): void {
    this.pop.hidden = false;
    this.search.value = "";
    // Mit der Kategorie des aktuellen Coins öffnen
    const cat = this.active ? this.markets.bySymbol.get(this.active)?.category : undefined;
    if (cat) this.category = cat;
    this.buildCategoryBar();
    this.update(Date.now());
    this.refresh(false);
    const i = this.visible.findIndex((r) => r.sym === this.active);
    this.setHighlight(i, true);
  }

  close(): void {
    this.pop.hidden = true;
  }

  private choose(sym: string): void {
    this.close();
    this.onSelect(sym);
  }

  private createRow(sym: string): Row {
    const b = el("button", "picker-row");
    b.type = "button";
    const dot = el("span", "dot");
    const name = el("span", "sym");
    const label = el("span", "sym-label", this.markets.bySymbol.get(sym)?.label ?? sym);
    const sess = el("span", "sess");
    const cat = el("span", "cat-tag");
    name.append(dot, label, sess, cat);
    const px = el("span", "num");
    const chg = el("span", "num");
    const ytd = el("span", "num");
    const rsi = el("span", "num");
    b.append(name, px, chg, ytd, rsi);
    b.addEventListener("click", () => this.choose(sym));
    return { sym, el: b, dot, name: label, sess, cat, px, chg, ytd, rsi, v: { price: null, chg: null, ytd: null, rsi: null } };
  }

  private categoryOf(sym: string): Category {
    return this.markets.bySymbol.get(sym)?.category ?? "crypto";
  }

  private buildCategoryBar(): void {
    const counts = new Map<Category, number>();
    for (const s of this.symbols) counts.set(this.categoryOf(s), (counts.get(this.categoryOf(s)) ?? 0) + 1);
    if (!counts.get(this.category)) this.category = CATEGORY_GROUPS.find((g) => counts.get(g.id))?.id ?? "crypto";
    this.catBar.replaceChildren();
    this.catButtons.clear();
    for (const g of CATEGORY_GROUPS) {
      const n = counts.get(g.id);
      if (!n) continue;
      const b = el("button", "picker-cat");
      b.type = "button";
      b.setAttribute("role", "tab");
      b.append(el("span", "", shortTitle(g.id)), el("span", "picker-cat-n", String(n)));
      b.addEventListener("click", () => this.setCategory(g.id));
      this.catButtons.set(g.id, b);
      this.catBar.append(b);
    }
    this.markCategory();
  }

  private markCategory(): void {
    const searching = this.search.value.trim() !== "";
    for (const [c, b] of this.catButtons) {
      const on = !searching && c === this.category;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", String(on));
    }
  }

  private setCategory(c: Category): void {
    this.category = c;
    this.search.value = "";
    try {
      localStorage.setItem(CAT_KEY, c);
    } catch {
      /* egal */
    }
    this.refresh(true);
  }

  private toggleSort(key: SortKey): void {
    if (this.sort.key === key) {
      // Zweiter Klick dreht um, dritter setzt zurück auf die Config-Reihenfolge
      if (this.sort.dir === (key === "name" ? 1 : -1)) this.sort = { key, dir: key === "name" ? -1 : 1 };
      else this.sort = { key: "default", dir: 1 };
    } else this.sort = { key, dir: key === "name" ? 1 : -1 }; // Zahlen: größte zuerst
    this.refresh(false);
  }

  /** Liste neu filtern/sortieren. `resetHighlight`: Auswahl auf die erste Zeile. */
  private refresh(resetHighlight: boolean): void {
    const q = this.search.value.trim().toUpperCase();
    let rows = this.symbols.map((s) => this.rows.get(s)!).filter(Boolean);
    if (q) {
      rows = rows.filter((r) => r.sym.includes(q) || (this.markets.bySymbol.get(r.sym)?.label ?? "").toUpperCase().includes(q));
    } else {
      rows = rows.filter((r) => this.categoryOf(r.sym) === this.category);
    }
    const { key, dir } = this.sort;
    if (key === "name") {
      rows.sort((a, b) => dir * a.name.textContent!.localeCompare(b.name.textContent!, "de"));
    } else if (key !== "default") {
      // fehlende Werte immer ans Ende
      rows.sort((a, b) => {
        const x = a.v[key];
        const y = b.v[key];
        if (x === null) return y === null ? 0 : 1;
        if (y === null) return -1;
        return dir * (x - y);
      });
    }
    for (const r of this.rows.values()) r.cat.textContent = q ? shortTitle(this.categoryOf(r.sym)) : "";
    this.list.replaceChildren(...rows.map((r) => r.el));
    this.visible = rows;
    this.empty.hidden = rows.length > 0;
    for (const [k, h] of this.heads) {
      h.classList.toggle("sorted", k === key);
      h.dataset.dir = k === key ? (dir > 0 ? "▲" : "▼") : "";
    }
    this.markCategory();
    this.updateSessionLine();
    this.list.scrollTop = resetHighlight ? 0 : this.list.scrollTop;
    this.setHighlight(resetHighlight ? (q ? 0 : -1) : Math.min(this.highlight, rows.length - 1), resetHighlight);
  }

  private setHighlight(i: number, scroll: boolean): void {
    this.highlight = Math.max(-1, Math.min(i, this.visible.length - 1));
    this.visible.forEach((r, j) => r.el.classList.toggle("hl", j === this.highlight));
    const r = this.visible[this.highlight];
    if (r && scroll) {
      // Nur die Liste scrollen (scrollIntoView würde auch die Seite verschieben)
      const top = r.el.offsetTop;
      const bottom = top + r.el.offsetHeight;
      if (top < this.list.scrollTop) this.list.scrollTop = top;
      else if (bottom > this.list.scrollTop + this.list.clientHeight) this.list.scrollTop = bottom - this.list.clientHeight;
    }
  }

  private onKey(e: KeyboardEvent): void {
    const inSearch = e.target === this.search;
    if (e.key === "Escape") this.close();
    else if (e.key === "ArrowDown") this.setHighlight(this.highlight + 1, true);
    else if (e.key === "ArrowUp") this.setHighlight(Math.max(0, this.highlight - 1), true);
    else if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && !(inSearch && this.search.value)) {
      const cats = [...this.catButtons.keys()];
      const i = cats.indexOf(this.category);
      const next = cats[(i + (e.key === "ArrowRight" ? 1 : -1) + cats.length) % cats.length];
      if (next) this.setCategory(next);
    } else if (e.key === "Enter") {
      const r = this.visible[Math.max(0, this.highlight)];
      if (r) this.choose(r.sym);
    } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey && !inSearch) {
      // Einfach lostippen: Suche bekommt den Fokus und das Zeichen; Chart-Kürzel
      // (C, F …) dürfen dabei nicht auslösen
      this.search.focus();
      e.stopPropagation();
      return;
    } else return;
    e.preventDefault();
    e.stopPropagation();
  }

  /** Handelszeiten der TradFi-Märkte in der gewählten Kategorie (eine Zeile). */
  private updateSessionLine(): void {
    const searching = this.search.value.trim() !== "";
    const markets = new Set<string>();
    if (!searching && this.category !== "crypto") {
      for (const s of this.symbols) {
        const m = this.markets.bySymbol.get(s);
        if (m?.category === this.category && m.market) markets.add(m.market);
      }
    }
    const names: Record<string, string> = {
      EQUITY: "US-Börse",
      KR_EQUITY: "Korea",
      HK_EQUITY: "Hongkong",
      CN_EQUITY: "China",
      COMMODITY: "Rohstoffe",
      FX: "Devisen",
    };
    this.sessLine.replaceChildren();
    for (const mk of markets) {
      const s = this.markets.sessions.get(mk) ?? null;
      const item = el("span", `sess-item ${sessionClass(s)}`);
      item.append(el("span", "dot"), `${names[mk] ?? mk}: ${sessionLong(s)}`);
      this.sessLine.append(item);
    }
    this.sessLine.hidden = markets.size === 0;
  }

  /** Werte aktualisieren (Knopf immer, Liste nur wenn offen). */
  update(now: number): void {
    const mk = this.markets;
    if (this.active) {
      const s = mk.bySymbol.get(this.active)?.sync;
      this.btnDot.className = `dot ${!s ? "" : s.synced ? "ok" : "bad"}`;
    }
    if (this.pop.hidden) return;
    for (const r of this.visible) {
      const m = mk.bySymbol.get(r.sym);
      const price = mk.price(r.sym);
      const dec = m?.decimals ?? 2;
      if (m) r.name.textContent = m.label;
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
      r.v = { price, chg, ytd, rsi };
      const session = mk.session(r.sym);
      r.sess.textContent = session ? sessionText(session) : "";
      r.sess.className = `sess ${sessionClass(session)}`;
      const s = m?.sync;
      r.dot.className = `dot ${!s ? "" : s.synced ? "ok" : "bad"}`;
      const sessTitle = m?.kind === "tradfi" ? ` · Handel: ${sessionLong(session)}` : "";
      r.el.title =
        (s
          ? `${r.sym}: Orderbuch ${s.synced ? "synchron" : "NICHT synchron"} · Neu-Syncs: ${s.resyncs}` +
            (s.reason ? ` · zuletzt: ${s.reason}` : "")
          : `${r.sym}: Orderbuch startet bei Auswahl`) + sessTitle;
    }
    this.updateSessionLine();
  }
}
