/**
 * Coin-Tabs wie im Browser: offene Coins nebeneinander, Klick wechselt, × oder
 * Mittelklick schließt, Ziehen sortiert um, "+" öffnet die Coin-Suche.
 * Tastatur: Alt+1…9 = Tab n, Alt+←/→ = vorheriger/nächster Tab, Alt+W = Tab schließen.
 * Die Liste wird im Browser gespeichert.
 */

import { sessionClass, sessionLong } from "./categories";
import type { Markets } from "./markets";

const KEY = "cockbook.tabs.v1";

/** Tab-IDs: "BTCUSDT" = Live-Chart, "BTCUSDT@long" = Langzeit-Heatmap des Coins. */
export const LONG_SUFFIX = "@long";
export const baseSymbol = (id: string) => id.split("@")[0]!;
export const isLongTab = (id: string) => id.endsWith(LONG_SUFFIX);

/** Reine Tab-Logik (ohne DOM, getestet). */
export class TabModel {
  tabs: string[] = [];
  active: string | null = null;

  /** Öffnet (falls nötig) und aktiviert; neue Tabs kommen rechts neben den aktiven. */
  open(sym: string): void {
    if (!this.tabs.includes(sym)) {
      const i = this.active ? this.tabs.indexOf(this.active) : -1;
      this.tabs.splice(i + 1, 0, sym);
    }
    this.active = sym;
  }

  /** Schließt; liefert den neuen aktiven Tab (rechter Nachbar, sonst linker). Der letzte
   *  Tab bleibt offen – ohne Tab gäbe es nichts anzuzeigen. */
  close(sym: string): string | null {
    const i = this.tabs.indexOf(sym);
    if (i < 0 || this.tabs.length <= 1) return this.active;
    this.tabs.splice(i, 1);
    if (this.active === sym) this.active = this.tabs[Math.min(i, this.tabs.length - 1)] ?? null;
    return this.active;
  }

  move(sym: string, toIndex: number): void {
    const i = this.tabs.indexOf(sym);
    if (i < 0) return;
    this.tabs.splice(i, 1);
    this.tabs.splice(Math.max(0, Math.min(toIndex, this.tabs.length)), 0, sym);
  }

  /** Nachbar-Tab (zyklisch); `dir` = +1 rechts, -1 links. */
  step(dir: 1 | -1): string | null {
    if (!this.tabs.length) return null;
    const i = this.active ? this.tabs.indexOf(this.active) : 0;
    return this.tabs[(i + dir + this.tabs.length) % this.tabs.length] ?? null;
  }

  /** Nicht (mehr) verfügbare Symbole entfernen, z. B. delistete Coins. */
  retain(available: Set<string>): void {
    this.tabs = this.tabs.filter((s) => available.has(s));
    if (this.active && !available.has(this.active)) this.active = this.tabs[0] ?? null;
  }

  load(): void {
    try {
      const raw = localStorage.getItem(KEY);
      if (raw) {
        const v = JSON.parse(raw) as { tabs?: unknown; active?: unknown };
        if (Array.isArray(v.tabs)) this.tabs = v.tabs.filter((x): x is string => typeof x === "string");
        if (typeof v.active === "string" && this.tabs.includes(v.active)) this.active = v.active;
      }
    } catch {
      /* privates Fenster o. Ä. */
    }
  }

  save(): void {
    try {
      localStorage.setItem(KEY, JSON.stringify({ tabs: this.tabs, active: this.active }));
    } catch {
      /* egal */
    }
  }
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

interface TabEl {
  root: HTMLElement;
  dot: HTMLElement;
  px: HTMLElement;
  chg: HTMLElement;
}

export interface TabCallbacks {
  onActivate(symbol: string): void;
  onAdd(): void;
}

export class TabBar {
  private els = new Map<string, TabEl>();
  private list: HTMLElement;
  private dragSym: string | null = null;

  constructor(
    root: HTMLElement,
    private markets: Markets,
    readonly model: TabModel,
    private cb: TabCallbacks,
  ) {
    this.list = el("div", "tabs-list");
    this.list.setAttribute("role", "tablist");
    const add = el("button", "tab-add", "+");
    add.type = "button";
    add.title = "Coin öffnen (Taste /)";
    add.addEventListener("click", () => this.cb.onAdd());
    root.replaceChildren(this.list, add);
    // Mausrad scrollt die Tab-Leiste waagerecht (bei vielen Tabs)
    this.list.addEventListener(
      "wheel",
      (e) => {
        if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
          this.list.scrollLeft += e.deltaY;
          e.preventDefault();
        }
      },
      { passive: false },
    );
    window.addEventListener("keydown", (e) => {
      if (!e.altKey || e.target instanceof HTMLInputElement) return;
      let target: string | null = null;
      if (e.key >= "1" && e.key <= "9") target = this.model.tabs[Number(e.key) - 1] ?? null;
      else if (e.key === "ArrowRight") target = this.model.step(1);
      else if (e.key === "ArrowLeft") target = this.model.step(-1);
      else if (e.key === "w" || e.key === "W") {
        if (this.model.active) this.close(this.model.active);
        e.preventDefault();
        return;
      } else return;
      e.preventDefault();
      if (target) this.cb.onActivate(target);
    });
  }

  /** Nach jeder Änderung des Modells aufrufen. */
  render(): void {
    const m = this.model;
    const wanted = new Set(m.tabs);
    for (const [sym, t] of this.els) {
      if (!wanted.has(sym)) {
        t.root.remove();
        this.els.delete(sym);
      }
    }
    m.tabs.forEach((sym, i) => {
      let t = this.els.get(sym);
      if (!t) t = this.create(sym);
      if (this.list.children[i] !== t.root) this.list.insertBefore(t.root, this.list.children[i] ?? null);
      const active = sym === m.active;
      t.root.classList.toggle("active", active);
      t.root.setAttribute("aria-selected", String(active));
      if (active) t.root.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
    m.save();
    this.update();
  }

  private create(sym: string): TabEl {
    const root = el("div", "tab");
    root.setAttribute("role", "tab");
    root.draggable = true;
    const dot = el("span", "dot");
    const base = baseSymbol(sym);
    const name = el("span", "tab-name", this.markets.bySymbol.get(base)?.label ?? base);
    const kind: HTMLElement[] = [];
    if (isLongTab(sym)) {
      root.classList.add("tab-long");
      kind.push(el("span", "tab-kind", "Langzeit"));
    }
    const px = el("span", "tab-px num");
    const chg = el("span", "tab-chg num");
    const close = el("button", "tab-close", "×");
    close.type = "button";
    close.title = "Tab schließen (Alt+W)";
    close.addEventListener("click", (e) => {
      e.stopPropagation();
      this.close(sym);
    });
    root.append(dot, name, ...kind, px, chg, close);
    root.addEventListener("click", () => this.cb.onActivate(sym));
    root.addEventListener("auxclick", (e) => {
      if (e.button === 1) this.close(sym);
    });
    root.addEventListener("dragstart", (e) => {
      this.dragSym = sym;
      e.dataTransfer?.setData("text/plain", sym);
      root.classList.add("dragging");
    });
    root.addEventListener("dragend", () => {
      this.dragSym = null;
      root.classList.remove("dragging");
    });
    root.addEventListener("dragover", (e) => {
      if (!this.dragSym || this.dragSym === sym) return;
      e.preventDefault();
      const rect = root.getBoundingClientRect();
      const after = e.clientX > rect.left + rect.width / 2;
      const to = this.model.tabs.indexOf(sym) + (after ? 1 : 0);
      const from = this.model.tabs.indexOf(this.dragSym);
      this.model.move(this.dragSym, from < to ? to - 1 : to);
      this.render();
    });
    const t = { root, dot, px, chg };
    this.els.set(sym, t);
    return t;
  }

  private close(sym: string): void {
    const wasActive = this.model.active === sym;
    const next = this.model.close(sym);
    this.render();
    if (wasActive && next) this.cb.onActivate(next);
  }

  /** Preise/Status aktualisieren (2x pro Sekunde). */
  update(): void {
    const mk = this.markets;
    for (const [id, t] of this.els) {
      const sym = baseSymbol(id);
      const m = mk.bySymbol.get(sym);
      const price = mk.price(sym);
      const dec = m?.decimals ?? 2;
      t.px.textContent =
        price === null ? "" : price.toLocaleString("de-DE", { minimumFractionDigits: dec, maximumFractionDigits: dec });
      const chg = mk.change24h(sym);
      t.chg.textContent =
        chg === null ? "" : `${chg > 0 ? "+" : ""}${chg.toLocaleString("de-DE", { maximumFractionDigits: 1, minimumFractionDigits: 1 })}%`;
      t.chg.className = `tab-chg num ${chg === null ? "" : chg > 0 ? "up" : chg < 0 ? "down" : ""}`;
      const s = m?.sync;
      const session = mk.session(sym);
      // Punkt: Orderbuch-Status; bei TradFi außerhalb der regulären Zeit Sitzungsfarbe
      t.dot.className = `dot ${!s ? "" : s.synced ? "ok" : "bad"} ${session ? `sess-${sessionClass(session)}` : ""}`;
      const label = t.root.querySelector(".tab-name");
      if (label && m) label.textContent = m.label;
      t.root.title =
        `${sym}` +
        (s ? ` · Orderbuch ${s.synced ? "synchron" : "NICHT synchron"}` : " · Orderbuch startet") +
        (m?.kind === "tradfi" ? ` · Handel: ${sessionLong(session)}` : "");
    }
  }
}
