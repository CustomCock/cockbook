/**
 * Seitenleiste links: Chart-Ebenen (inkl. Kerzen), ATH & Zyklen, Indikatoren (RSI),
 * Derivate-Kennzahlen, Liquidationen (Gesamtmarkt) und geplante Funktionen.
 * Die Struktur wird einmal aufgebaut; `update()` setzt nur Inhalte neu (2x pro Sekunde).
 */

import { aggregate, CANDLE_INTERVALS, type Bar, type CandleMinutes } from "./candles";
import { athStats, CYCLE_PRESETS, cycleStats, DAY_MS, parseDate } from "./cycles";
import { FEATURES, type FeatureState } from "./features";
import { liqPerSecond, topLiquidated, type Markets } from "./markets";
import { lastRsi, RSI_OVERBOUGHT, RSI_OVERSOLD, RSI_PERIOD, rsiZone } from "./rsi";

export interface CycleChoice {
  id: string; // Preset-ID oder "custom"
  start: string; // YYYY-MM-DD
  end: string;
}

export interface SidebarCallbacks {
  /** 1m-Kerzen des gewählten Coins (für die RSI-Übersicht) */
  candles1m(): Bar[];
  onToggle(id: string, on: boolean): void;
  onCandleInterval(minutes: CandleMinutes): void;
  onCycle(choice: CycleChoice): void;
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

function fmtDate(ms: number): string {
  return new Date(ms).toLocaleDateString("de-DE", { timeZone: "UTC" });
}

function fmtDays(d: number): string {
  return `${d.toLocaleString("de-DE")}${NBSP}Tage`;
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

/** Fortschrittsbalken 0..100 % */
function progress(pct: number): HTMLElement {
  const wrap = el("div", "progress");
  const fill = el("div", "progress-fill");
  fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  wrap.append(fill);
  return wrap;
}

export class Sidebar {
  private derivTitle: HTMLElement;
  private histTitle: HTMLElement;
  private indTitle: HTMLElement;
  private values = new Map<string, HTMLElement>();
  private active: string | null = null;
  private cycleSelect!: HTMLSelectElement;
  private cycleStart!: HTMLInputElement;
  private cycleEnd!: HTMLInputElement;
  private customBox!: HTMLElement;
  private liqBars: HTMLElement[] = [];

  constructor(
    root: HTMLElement,
    private markets: Markets,
    private features: FeatureState,
    private cb: SidebarCallbacks,
    candleMinutes: CandleMinutes,
    private cycle: CycleChoice,
  ) {
    root.replaceChildren();

    // Chart-Ebenen
    const chart = el("section");
    chart.append(el("h3", "", "Chart"));
    for (const f of FEATURES.filter((x) => x.group === "chart")) {
      const t = this.toggle(f.id, f.label, f.hint);
      if (f.id === "candles") {
        const row = el("div", "toggle-row");
        row.append(t, this.intervalPicker(candleMinutes));
        chart.append(row);
      } else chart.append(t);
    }

    // ATH & Zyklen
    const hist = el("section");
    this.histTitle = el("h3", "", "ATH & Zyklen");
    hist.append(this.histTitle);
    for (const f of FEATURES.filter((x) => x.group === "history")) {
      const item = el("div", "feat");
      item.append(this.toggle(f.id, f.label, f.hint));
      const val = el("div", "feat-val");
      val.hidden = !this.features[f.id];
      if (f.id === "cycles") {
        const ctl = this.cycleControls();
        ctl.hidden = !this.features[f.id];
        this.values.set("cyclesCtl", ctl);
        item.append(ctl);
      }
      this.values.set(f.id, val);
      item.append(val);
      hist.append(item);
    }

    // Indikatoren
    const ind = el("section");
    this.indTitle = el("h3", "", "Indikatoren");
    ind.append(this.indTitle);
    for (const f of FEATURES.filter((x) => x.group === "indicators")) {
      const item = el("div", "feat");
      item.append(this.toggle(f.id, f.label, f.hint));
      const val = el("div", "feat-val");
      val.hidden = !this.features[f.id];
      this.values.set(f.id, val);
      item.append(val);
      ind.append(item);
    }

    // Derivate
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

    // Geplant
    const planned = el("section", "planned");
    planned.append(el("h3", "", "Geplant (wie Coinank)"));
    for (const f of FEATURES.filter((x) => x.group === "planned")) {
      const item = el("div", "planned-item");
      item.append(el("div", "", f.label), el("div", "hint", f.hint));
      planned.append(item);
    }

    root.append(chart, hist, ind, deriv, planned);
  }

  private toggle(id: string, label: string, hint: string): HTMLElement {
    const lab = el("label", "toggle");
    lab.title = hint;
    const box = el("input");
    box.type = "checkbox";
    box.checked = this.features[id] ?? false;
    box.addEventListener("change", () => {
      this.features[id] = box.checked;
      for (const key of [id, `${id}Ctl`]) {
        const v = this.values.get(key);
        if (v) v.hidden = !box.checked;
      }
      this.cb.onToggle(id, box.checked);
    });
    lab.append(box, el("span", "", label));
    return lab;
  }

  private intervalPicker(current: CandleMinutes): HTMLElement {
    const box = el("div", "seg");
    const buttons: HTMLButtonElement[] = [];
    for (const m of CANDLE_INTERVALS) {
      const b = el("button", m === current ? "on" : "", `${m}m`);
      b.type = "button";
      b.addEventListener("click", () => {
        for (const x of buttons) x.classList.toggle("on", x === b);
        this.cb.onCandleInterval(m);
      });
      buttons.push(b);
      box.append(b);
    }
    return box;
  }

  private cycleControls(): HTMLElement {
    const box = el("div", "cycle-ctl");
    this.cycleSelect = el("select");
    for (const p of CYCLE_PRESETS) {
      const o = el("option", "", p.label);
      o.value = p.id;
      this.cycleSelect.append(o);
    }
    const custom = el("option", "", "Eigener Zeitraum …");
    custom.value = "custom";
    this.cycleSelect.append(custom);
    this.cycleSelect.value = this.cycle.id;

    this.customBox = el("div", "cycle-dates");
    this.cycleStart = el("input");
    this.cycleStart.type = "date";
    this.cycleStart.value = this.cycle.start;
    this.cycleEnd = el("input");
    this.cycleEnd.type = "date";
    this.cycleEnd.value = this.cycle.end;
    this.customBox.append(el("span", "", "von"), this.cycleStart, el("span", "", "bis"), this.cycleEnd);
    this.customBox.hidden = this.cycle.id !== "custom";

    const emit = () => {
      const id = this.cycleSelect.value;
      const preset = CYCLE_PRESETS.find((p) => p.id === id);
      if (preset) {
        this.cycleStart.value = preset.start;
        this.cycleEnd.value = preset.end;
      }
      this.customBox.hidden = id !== "custom";
      this.cycle = { id, start: this.cycleStart.value, end: this.cycleEnd.value };
      this.cb.onCycle(this.cycle);
    };
    this.cycleSelect.addEventListener("change", emit);
    for (const i of [this.cycleStart, this.cycleEnd]) {
      i.addEventListener("change", () => {
        this.cycleSelect.value = "custom";
        emit();
      });
    }
    box.append(this.cycleSelect, this.customBox);
    return box;
  }

  setActive(symbol: string): void {
    this.active = symbol;
    const label = this.markets.bySymbol.get(symbol)?.label ?? base(symbol);
    this.derivTitle.textContent = `Derivate · ${label}`;
    this.histTitle.textContent = `ATH & Zyklen · ${label}`;
    this.indTitle.textContent = `Indikatoren · ${label}`;
  }

  update(now: number): void {
    const mk = this.markets;
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
    const price = mk.price(sym);

    // --- ATH & Jahr --------------------------------------------------------------------
    const ath = m?.daily ? athStats(m.daily, price, now) : null;
    if (ath) {
      set(
        "ath",
        line("Allzeithoch", px(ath.ath)),
        line("am", ath.athTs >= now - DAY_MS ? "heute" : fmtDate(ath.athTs)),
        line("Abstand zum ATH", fmtPct(ath.fromAthPct), cls(ath.fromAthPct)),
        line("Tage seit ATH", fmtDays(ath.daysSinceAth)),
        line("seit 1. Januar", fmtPct(ath.ytdPct), cls(ath.ytdPct)),
        line("52 Wochen Hoch", px(ath.high52w)),
        line("52 Wochen Tief", px(ath.low52w)),
        line("Allzeittief*", `${px(ath.atl)} (${fmtDate(ath.atlTs)})`),
        el("div", "hint", `* seit Datenbeginn ${fmtDate(ath.dataStart)} (Binance Spot)`),
      );
    } else set("ath", "lade Tageshistorie …");

    // --- Zyklus ------------------------------------------------------------------------
    const start = parseDate(this.cycle.start);
    const end = parseDate(this.cycle.end);
    const c = m?.daily ? cycleStats(m.daily, start, end, now, price) : null;
    const preset = CYCLE_PRESETS.find((p) => p.id === this.cycle.id);
    if (c) {
      const nodes: (Node | string)[] = [
        line("Zeitraum", `${fmtDate(c.start)} – ${fmtDate(c.end)}${preset?.estimatedEnd ? "*" : ""}`),
        line(
          "Länge",
          c.complete ? fmtDays(c.lengthDays) : `Tag ${c.elapsedDays.toLocaleString("de-DE")} von ${c.lengthDays.toLocaleString("de-DE")}`,
        ),
      ];
      if (!c.complete) nodes.push(progress(c.progressPct), line("Fortschritt", fmtPct(c.progressPct, 1, false)));
      nodes.push(
        line(c.partial ? `Startpreis (${fmtDate(c.firstDataTs)})` : "Startpreis", px(c.startPrice)),
        line("Zyklus-Hoch", px(c.high)),
        line("  am", `${fmtDate(c.highTs)} (Tag ${c.highDay})`),
        line("Zyklus-Tief", px(c.low)),
        line("  am", `${fmtDate(c.lowTs)} (Tag ${c.lowDay})`),
        line("Start → Hoch", fmtPct(c.startToHighPct), cls(c.startToHighPct)),
        line(c.complete ? "Hoch → Ende" : "Hoch → jetzt", fmtPct(c.highToEndPct), cls(c.highToEndPct)),
        line(c.complete ? "Start → Ende" : "Start → jetzt", fmtPct(c.startToEndPct), cls(c.startToEndPct)),
      );
      if (c.partial) nodes.push(el("div", "hint", `Daten erst ab ${fmtDate(c.firstDataTs)} – Zyklus nur teilweise abgedeckt`));
      if (preset?.estimatedEnd) nodes.push(el("div", "hint", "* Ende geschätzt (nächstes Halving ≈ April 2028)"));
      set("cycles", ...nodes);
    } else set("cycles", m?.daily ? "keine Daten im gewählten Zeitraum" : "lade Tageshistorie …");

    // --- RSI ---------------------------------------------------------------------------
    const rsiVal = this.values.get("rsiOverview");
    if (rsiVal && !rsiVal.hidden) {
      const bars = this.cb.candles1m();
      const frames: [string, number | null][] = [1, 5, 15, 60].map((mins) => [
        mins < 60 ? `${mins}m` : `${mins / 60}h`,
        lastRsi(aggregate(bars, mins).map((b) => b.c)),
      ]);
      frames.push(["1D", mk.rsi1d(sym, now)]);
      const rows = frames.map(([label, v]) => {
        const r = el("div", "rsi-row");
        const track = el("div", "rsi-track");
        const mark = el("div", `rsi-mark ${rsiZone(v)}`);
        mark.hidden = v === null;
        if (v !== null) mark.style.left = `${v}%`;
        track.append(mark);
        r.append(el("span", "k", label), track, el("span", `v num rsi ${rsiZone(v)}`, v === null ? "–" : v.toFixed(1)));
        return r;
      });
      set(
        "rsiOverview",
        ...rows,
        el(
          "div",
          "hint",
          `RSI(${RSI_PERIOD}), Wilder · ≥ ${RSI_OVERBOUGHT} überkauft, ≤ ${RSI_OVERSOLD} überverkauft · ` +
            "Kurzfristige Rahmen aus den 1m-Kerzen der letzten 25 h (1h ist daher nur grob eingeschwungen)",
        ),
      );
    }

    // --- Derivate ----------------------------------------------------------------------
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
      const usd = m.oi.oi * (price ?? 0);
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

    // --- Liquidationen (Gesamtmarkt) -----------------------------------------------------
    const liqVal = this.values.get("liqFeed");
    if (liqVal && !liqVal.hidden) this.updateLiquidations(liqVal, now, line);
  }

  private updateLiquidations(
    box: HTMLElement,
    now: number,
    line: (l: string, v: string, c?: string) => HTMLElement,
  ): void {
    const mk = this.markets;
    // Balken je Sekunde: Struktur einmal anlegen, danach nur Höhen setzen
    if (!this.liqBars.length) {
      box.replaceChildren();
      const chart = el("div", "liq-chart");
      chart.title = "Liquidationen je Sekunde, letzte 60 s (oben Longs, unten Shorts)";
      for (let i = 0; i < 60; i++) {
        const col = el("div", "liq-col");
        const upHalf = el("div", "liq-half up");
        const downHalf = el("div", "liq-half down");
        upHalf.append(el("div", "liq-long-bar"));
        downHalf.append(el("div", "liq-short-bar"));
        col.append(upHalf, downHalf);
        chart.append(col);
        this.liqBars.push(col);
      }
      box.append(chart, el("div", "liq-info"));
    }
    const perSec = liqPerSecond(mk.liqs, now, 60);
    const max = Math.max(1, ...perSec.map((s) => Math.max(s.long, s.short)));
    perSec.forEach((s, i) => {
      const col = this.liqBars[i]!;
      (col.children[0]!.firstChild as HTMLElement).style.height = `${(s.long / max) * 100}%`;
      (col.children[1]!.firstChild as HTMLElement).style.height = `${(s.short / max) * 100}%`;
      col.title = `vor ${59 - i} s · Longs ${fmtCompact(s.long)} $ · Shorts ${fmtCompact(s.short)} $`;
    });

    const m1 = mk.liqSums(now, undefined, 60_000);
    const h1 = mk.liqSums(now);
    const info = box.querySelector(".liq-info") as HTMLElement;
    const top = topLiquidated(mk.liqs, now, 3600_000, 5);
    const topBox = el("div", "liq-top");
    for (const t of top) topBox.append(line(base(t.sym), `${fmtCompact(t.usd)} $`));
    const list = el("div", "liq-list");
    for (const l of mk.liqs.slice(0, 10)) {
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
    info.replaceChildren(
      line("Longs · 1 min", `${fmtCompact(m1.long)} $`, "liq-long"),
      line("Shorts · 1 min", `${fmtCompact(m1.short)} $`, "liq-short"),
      line("Longs · 1 h", `${fmtCompact(h1.long)} $`, "liq-long"),
      line("Shorts · 1 h", `${fmtCompact(h1.short)} $`, "liq-short"),
      el("div", "sub", "Top-Coins (1 h)"),
      topBox,
      el("div", "sub", "Letzte"),
      list,
    );
  }
}
