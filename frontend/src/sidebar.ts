/**
 * Seitenleiste links: Coin-Auswahl, Chart-Ebenen, Derivate-Kennzahlen und geplante
 * Funktionen. Die Struktur wird einmal aufgebaut; `update()` setzt nur Texte neu
 * (2x pro Sekunde), egal wie viele Kennzahl-Frames ankommen.
 */

import { FEATURES, type FeatureState } from "./features";
import type { Markets } from "./markets";

export interface SidebarCallbacks {
  onSelect(symbol: string): void;
  onToggle(id: string, on: boolean): void;
}

const NBSP = " ";

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  cls?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

export function fmtCompact(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e9) return `${(v / 1e9).toLocaleString("de-DE", { maximumFractionDigits: 2 })} Mrd`;
  if (a >= 1e6) return `${(v / 1e6).toLocaleString("de-DE", { maximumFractionDigits: 2 })} Mio`;
  if (a >= 1e3) return `${(v / 1e3).toLocaleString("de-DE", { maximumFractionDigits: 1 })} k`;
  return v.toLocaleString("de-DE", { maximumFractionDigits: 2 });
}

export function fmtPct(v: number | null, digits = 2, sign = true): string {
  if (v === null || !Number.isFinite(v)) return "–";
  const s = v.toLocaleString("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return `${sign && v > 0 ? "+" : ""}${s}${NBSP}%`;
}

function fmtCountdown(ms: number): string {
  if (ms < 0) return "00:00:00";
  const s = Math.floor(ms / 1000);
  const p2 = (n: number) => String(n).padStart(2, "0");
  return `${p2(Math.floor(s / 3600))}:${p2(Math.floor((s % 3600) / 60))}:${p2(s % 60)}`;
}

function fmtTime(ms: number): string {
  const d = new Date(ms);
  const p2 = (n: number) => String(n).padStart(2, "0");
  return `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
}

const base = (sym: string) => sym.replace(/USDT$|USDC$|USD$/, "");

function cls(v: number | null): string {
  return v === null ? "" : v > 0 ? "up" : v < 0 ? "down" : "";
}

/** Zweifarbiger Balken (z. B. Long/Short). */
function bar(left: number, right: number, leftLabel: string, rightLabel: string): HTMLElement {
  const total = left + right || 1;
  const wrap = el("div", "bar");
  const l = el("div", "bar-l");
  const r = el("div", "bar-r");
  l.style.width = `${(left / total) * 100}%`;
  r.style.width = `${(right / total) * 100}%`;
  wrap.append(l, r);
  const labels = el("div", "bar-labels");
  labels.append(el("span", "up", leftLabel), el("span", "down", rightLabel));
  const box = el("div");
  box.append(wrap, labels);
  return box;
}

export class Sidebar {
  private coinRows = new Map<string, { row: HTMLElement; px: HTMLElement; chg: HTMLElement; fr: HTMLElement }>();
  private coinsBox: HTMLElement;
  private derivTitle: HTMLElement;
  private values = new Map<string, HTMLElement>();
  private active: string | null = null;

  constructor(
    root: HTMLElement,
    private markets: Markets,
    private features: FeatureState,
    private cb: SidebarCallbacks,
  ) {
    root.replaceChildren();

    const coins = el("section");
    coins.append(el("h3", "", "Coins"));
    const head = el("div", "coin coin-head");
    head.append(el("span", "", "Symbol"), el("span", "", "Preis"), el("span", "", "24h"), el("span", "", "Funding"));
    this.coinsBox = el("div", "coins");
    coins.append(head, this.coinsBox);

    const chart = el("section");
    chart.append(el("h3", "", "Chart"));
    for (const f of FEATURES.filter((x) => x.group === "chart")) chart.append(this.toggle(f.id, f.label, f.hint));

    const deriv = el("section");
    this.derivTitle = el("h3", "", "Derivate");
    deriv.append(this.derivTitle);
    for (const f of FEATURES.filter((x) => x.group === "derivatives")) {
      const item = el("div", "feat");
      item.append(this.toggle(f.id, f.label, f.hint));
      const val = el("div", "feat-val");
      val.hidden = !this.features[f.id];
      this.values.set(f.id, val);
      item.append(val);
      deriv.append(item);
    }

    const planned = el("section", "planned");
    planned.append(el("h3", "", "Geplant (wie Coinank)"));
    for (const f of FEATURES.filter((x) => x.group === "planned")) {
      const item = el("div", "planned-item");
      item.append(el("div", "", f.label), el("div", "hint", f.hint));
      planned.append(item);
    }

    root.append(coins, chart, deriv, planned);
  }

  private toggle(id: string, label: string, hint: string): HTMLElement {
    const lab = el("label", "toggle");
    lab.title = hint;
    const box = el("input");
    box.type = "checkbox";
    box.checked = this.features[id] ?? false;
    box.addEventListener("change", () => {
      this.features[id] = box.checked;
      const val = this.values.get(id);
      if (val) val.hidden = !box.checked;
      this.cb.onToggle(id, box.checked);
    });
    lab.append(box, el("span", "", label));
    return lab;
  }

  setSymbols(symbols: string[]): void {
    const known = [...this.coinRows.keys()];
    if (known.length === symbols.length && known.every((s, i) => s === symbols[i])) return;
    this.coinsBox.replaceChildren();
    this.coinRows.clear();
    for (const sym of symbols) {
      const row = el("button", "coin");
      row.type = "button";
      row.title = sym;
      const px = el("span", "num");
      const chg = el("span", "num");
      const fr = el("span", "num");
      row.append(el("span", "sym", base(sym)), px, chg, fr);
      row.addEventListener("click", () => this.cb.onSelect(sym));
      this.coinsBox.append(row);
      this.coinRows.set(sym, { row, px, chg, fr });
    }
    if (this.active) this.setActive(this.active);
  }

  setActive(symbol: string): void {
    this.active = symbol;
    for (const [sym, r] of this.coinRows) r.row.classList.toggle("active", sym === symbol);
    this.derivTitle.textContent = `Derivate · ${base(symbol)}`;
  }

  update(now: number): void {
    const mk = this.markets;
    for (const [sym, r] of this.coinRows) {
      const m = mk.bySymbol.get(sym);
      const price = mk.price(sym);
      r.px.textContent = price === null ? "–" : price.toLocaleString("de-DE", {
        minimumFractionDigits: m?.decimals ?? 2,
        maximumFractionDigits: m?.decimals ?? 2,
      });
      const chg = mk.change24h(sym);
      r.chg.textContent = fmtPct(chg);
      r.chg.className = `num ${cls(chg)}`;
      const fr = m?.mark ? m.mark.fr * 100 : null;
      r.fr.textContent = fmtPct(fr, 4);
      r.fr.className = `num ${cls(fr)}`;
    }
    const sym = this.active;
    if (!sym) return;
    const m = mk.bySymbol.get(sym);
    const dec = m?.decimals ?? 2;
    const px = (v: number) => v.toLocaleString("de-DE", { minimumFractionDigits: dec, maximumFractionDigits: dec });
    const set = (id: string, ...nodes: (Node | string)[]) => {
      const v = this.values.get(id);
      if (v && !v.hidden) v.replaceChildren(...nodes);
    };
    const line = (label: string, value: string, c = "") => {
      const d = el("div", "kv");
      d.append(el("span", "k", label), el("span", `v ${c}`, value));
      return d;
    };

    if (m?.mark) {
      set(
        "markPrice",
        line("Mark", px(m.mark.mp)),
        line("Index", m.mark.ip !== null ? px(m.mark.ip) : "–"),
        line("Basis", m.mark.ip ? fmtPct(((m.mark.mp - m.mark.ip) / m.mark.ip) * 100, 3) : "–"),
      );
      const fr = m.mark.fr * 100;
      set(
        "funding",
        line("Rate", fmtPct(fr, 4), cls(fr)),
        line("≈ p. a. (8h-Intervall)", fmtPct(fr * 3 * 365, 1), cls(fr)),
        line("Nächstes Funding", m.mark.nft ? fmtCountdown(m.mark.nft - now) : "–"),
      );
    } else {
      set("markPrice", "warte auf Daten …");
      set("funding", "warte auf Daten …");
    }

    if (m?.oi) {
      const usd = m.oi.oi * (mk.price(sym) ?? 0);
      const c5 = mk.oiChange(sym, 5);
      const c60 = mk.oiChange(sym, 60);
      set(
        "openInterest",
        line("OI", `${fmtCompact(m.oi.oi)} ${base(sym)}`),
        line("Wert", `${fmtCompact(usd)} USDT`),
        line("Δ 5 min", c5 === null ? "sammle …" : fmtPct(c5), cls(c5)),
        line("Δ 1 h", c60 === null ? "sammle …" : fmtPct(c60), cls(c60)),
      );
    } else set("openInterest", "warte auf Daten …");

    if (m?.ls) {
      set(
        "longShort",
        bar(m.ls.long, m.ls.short, `Long ${fmtPct(m.ls.long * 100, 1, false)}`, `Short ${fmtPct(m.ls.short * 100, 1, false)}`),
        line(`Ratio (${m.ls.p})`, m.ls.ratio.toLocaleString("de-DE", { maximumFractionDigits: 3 })),
      );
    } else set("longShort", "warte auf Daten (alle 5 min) …");

    if (m?.tv) {
      set(
        "takerVolume",
        bar(m.tv.buy, m.tv.sell, `Kauf ${fmtCompact(m.tv.buy)}`, `Verkauf ${fmtCompact(m.tv.sell)}`),
        line(`Buy/Sell (${m.tv.p})`, m.tv.ratio.toLocaleString("de-DE", { maximumFractionDigits: 3 })),
      );
    } else set("takerVolume", "warte auf Daten (alle 5 min) …");

    if (m?.ticker) {
      set(
        "ticker24h",
        line("Hoch", px(m.ticker.high)),
        line("Tief", px(m.ticker.low)),
        line("Volumen", `${fmtCompact(m.ticker.qvol)} USDT`),
      );
    } else set("ticker24h", "warte auf Daten …");

    const all = mk.liqSums(now);
    const list = el("div", "liq-list");
    for (const l of mk.liqs.slice(0, 12)) {
      const row = el("div", `liq ${l.side === -1 ? "long" : "short"}`);
      row.append(
        el("span", "", fmtTime(l.ts)),
        el("span", "", base(l.sym)),
        el("span", "", l.side === -1 ? "Long" : "Short"),
        el("span", "num", `${fmtCompact(l.usd)} $`),
      );
      list.append(row);
    }
    if (!mk.liqs.length) list.append(el("div", "hint", "noch keine Liquidationen"));
    set(
      "liqFeed",
      line("Longs liquidiert (1 h)", `${fmtCompact(all.long)} $`, "liq-long"),
      line("Shorts liquidiert (1 h)", `${fmtCompact(all.short)} $`, "liq-short"),
      list,
    );
  }
}
