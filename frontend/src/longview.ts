/**
 * Langzeit-Heatmap als eigener Tab: 12h / 1D / 1W / 1M.
 *
 * Jede Stufe kommt vom Backend mit eigener Auflösung (z. B. 1D = 1-min-Spalten, 1M =
 * 30-min-Spalten, Preis-Buckets entsprechend gröber), gespeichert unter data/longterm –
 * so hat jeder Zeitraum ~1000–1500 Spalten und passt komplett in eine GPU-Textur.
 * Darüber: Kerzen im passenden Intervall (5m/15m/1h/4h), darunter Liquidationen je Spalte.
 *
 * Eigenes Canvas mit eigenem WebGL-Kontext: Die Live-Ansicht bleibt unberührt und muss
 * beim Hin- und Herschalten nichts neu hochladen.
 */

import { AXIS_H, AXIS_W, drawPriceAxis, drawTimeAxis, dateLabel, formatPrice, gridStepTicks, timeLabel, type Instrument } from "./axes";
import { buildCandles, RRectBuilder, type Bar } from "./candles";
import { computeRange, type ScaleRange, type ScaleSettings } from "./colorscale";
import { HeatmapRenderer, PRIM, PrimRenderer, RRectRenderer } from "./gl";
import { buildDomRows, DOM_W, drawDom } from "./dom";
import { decimalsOf, type Markets } from "./markets";
import { toFloat32, toInt32, type Connection, type LongHello, type LongMessage } from "./net";
import { buildBbo, buildGrid, LINE, VertexBuilder, type SceneParams } from "./scene";
import { HeatmapStore } from "./store";
import { colToX, panY, visibleCols, xToCol, yToUnit, zoomX, zoomY, type View } from "./viewport";

export const LONG_TIERS = ["12h", "1D", "1W", "1M"] as const;
const LIQ_H = 74;
const ROWS = 4096;
const KLINE_REFRESH_MS = 60_000;
/** Kleinster automatisch gezeigter Zeitraum: 1 h bzw. 24 Spalten (1M: 12 h) */
const MIN_FIT_MS = 3_600_000;
const MIN_FIT_COLS = 24;

export interface LongLayers {
  heat: boolean;
  candles: boolean;
  liqs: boolean;
  /** Orderbuch (Ø-Buch der Spalte am rechten Rand) neben der Preisachse */
  dom: boolean;
}

const $el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

function fmtUsd(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e9) return `${(v / 1e9).toLocaleString("de-DE", { maximumFractionDigits: 2 })} Mrd $`;
  if (a >= 1e6) return `${(v / 1e6).toLocaleString("de-DE", { maximumFractionDigits: 2 })} Mio $`;
  if (a >= 1e3) return `${(v / 1e3).toLocaleString("de-DE", { maximumFractionDigits: 1 })} k$`;
  return `${v.toLocaleString("de-DE", { maximumFractionDigits: 0 })} $`;
}

function intervalMs(iv: string): number {
  const units: Record<string, number> = { m: 60_000, h: 3_600_000, d: 86_400_000, w: 604_800_000 };
  return (Number(iv.slice(0, -1)) || 1) * (units[iv.slice(-1)] ?? 60_000);
}

export class LongView {
  readonly root: HTMLElement;
  private glCanvas: HTMLCanvasElement;
  private yAxis: HTMLCanvasElement;
  private xAxis: HTMLCanvasElement;
  private liqCanvas: HTMLCanvasElement;
  private domCanvas: HTMLCanvasElement;
  private plot: HTMLElement;
  private crossV: HTMLElement;
  private crossH: HTMLElement;
  private tooltip: HTMLElement;
  private message: HTMLElement;
  private info: HTMLElement;
  private tierButtons = new Map<string, HTMLButtonElement>();
  private heat: HeatmapRenderer;
  private prims: PrimRenderer;
  private rrects: RRectRenderer;
  private vGrid = new VertexBuilder(1024);
  private vBbo = new VertexBuilder(16384);
  private vCandles = new RRectBuilder();

  symbol: string | null = null;
  tier = "1D";
  layers: LongLayers = { heat: true, candles: true, liqs: true, dom: true };
  /** Zeitraum folgt den Daten (bis der Nutzer selbst horizontal zoomt/zieht) */
  private autoFit = true;
  private hello: LongHello | null = null;
  private store: HeatmapStore | null = null;
  private bars: Bar[] = [];
  private barMs = 60_000;
  private liq = new Map<number, [number, number]>();
  private view: View = { yLo: 0, yHi: 100, colWidth: 1, offset: 0, autoCenter: true };
  private autoY = true;
  private range: ScaleRange | null = null;
  private visible = false;
  private version = 0;
  private drawnVersion = -1;
  private dpr = window.devicePixelRatio || 1;
  private mouse: { x: number; y: number } | null = null;
  private drag: { x: number; y: number; view: View } | null = null;
  private klineTimer: ReturnType<typeof setInterval> | null = null;
  private loading = false;

  constructor(
    root: HTMLElement,
    private markets: Markets,
    private conn: Connection,
    private scale: () => ScaleSettings,
    private onTierChange: (tier: string) => void,
    private domUsd: () => boolean = () => false,
  ) {
    this.root = root;
    root.classList.add("longview");
    // Kopfleiste: Zeitraum + Info
    const bar = $el("div", "long-bar");
    const seg = $el("div", "seg long-tiers");
    for (const t of LONG_TIERS) {
      const b = $el("button", "", t);
      b.type = "button";
      b.title = `Heatmap der letzten ${t === "12h" ? "12 Stunden" : t === "1D" ? "24 Stunden" : t === "1W" ? "7 Tage" : "31 Tage"}`;
      b.addEventListener("click", () => this.setTier(t));
      this.tierButtons.set(t, b);
      seg.append(b);
    }
    this.info = $el("span", "long-info");
    bar.append($el("span", "long-title", "Langzeit-Heatmap"), seg, this.info);
    this.plot = $el("div", "long-plot");
    this.glCanvas = $el("canvas");
    this.yAxis = $el("canvas");
    this.xAxis = $el("canvas");
    this.liqCanvas = $el("canvas");
    this.domCanvas = $el("canvas");
    this.crossV = $el("div", "cross cross-v");
    this.crossH = $el("div", "cross cross-h");
    this.tooltip = $el("div", "long-tooltip");
    this.message = $el("div", "long-message");
    this.plot.append(this.glCanvas, this.yAxis, this.domCanvas, this.liqCanvas, this.xAxis, this.crossV, this.crossH, this.tooltip, this.message);
    root.append(bar, this.plot);

    this.heat = new HeatmapRenderer(this.glCanvas);
    this.prims = new PrimRenderer(this.heat.gl);
    this.rrects = new RRectRenderer(this.heat.gl);
    new ResizeObserver(() => this.resize()).observe(this.plot);
    this.bindInput();
    const frame = () => {
      requestAnimationFrame(frame);
      if (this.visible && this.drawnVersion !== this.version) {
        this.drawnVersion = this.version;
        this.render();
      }
    };
    requestAnimationFrame(frame);
    setInterval(() => this.updateRange(), 1000);
  }

  // --- Steuerung -----------------------------------------------------------------------

  /** Tab öffnen bzw. Coin/Stufe wechseln. */
  open(symbol: string, tier?: string): void {
    const t = tier ?? this.tier;
    this.visible = true;
    this.root.hidden = false;
    if (symbol === this.symbol && t === this.tier && this.hello) {
      this.resize();
      this.dirty();
      return;
    }
    this.symbol = symbol;
    this.tier = t;
    this.reset();
    this.conn.longOpen(symbol, t);
    if (this.klineTimer) clearInterval(this.klineTimer);
    this.klineTimer = setInterval(() => {
      if (this.visible && this.symbol) this.conn.longKlines(this.symbol, this.tier);
    }, KLINE_REFRESH_MS);
    this.resize();
  }

  setPalette(name: string): void {
    this.heat.setPalette(name);
    this.dirty();
  }

  hide(): void {
    this.visible = false;
    this.root.hidden = true;
  }

  /** Langzeit-Abo beim Server beenden (Tab geschlossen / Live-Tab aktiv). */
  stop(): void {
    this.hide();
    this.conn.longOff();
    this.symbol = null;
    this.hello = null;
    if (this.klineTimer) clearInterval(this.klineTimer);
    this.klineTimer = null;
  }

  setTier(t: string): void {
    if (!this.symbol || t === this.tier) return;
    this.onTierChange(t);
    this.open(this.symbol, t);
  }

  setLayers(l: LongLayers): void {
    const relayout = l.dom !== this.layers.dom || l.liqs !== this.layers.liqs;
    this.layers = l;
    if (relayout) this.resize();
    this.dirty();
  }

  private reset(): void {
    this.hello = null;
    this.store = null;
    this.bars = [];
    this.liq.clear();
    this.range = null;
    this.autoY = true;
    this.autoFit = true;
    this.loading = true;
    this.view = { ...this.view, offset: 0 };
    for (const [t, b] of this.tierButtons) b.classList.toggle("on", t === this.tier);
    this.showMessage("lade Langzeit-Daten …");
    this.dirty();
  }

  private dirty(): void {
    this.version++;
  }

  // --- Daten ---------------------------------------------------------------------------

  onMessage(m: LongMessage): void {
    if ("type" in m) {
      if (m.symbol !== this.symbol || m.tier !== this.tier) return;
      if (m.type === "long_hello") {
        this.hello = m;
        this.barMs = intervalMs(m.candle_interval);
        const cols = Math.ceil(m.span_ms / m.interval_ms) + 64;
        this.store = m.bucket_ticks ? new HeatmapStore(Math.min(cols, this.heat.maxTextureSize), Math.min(ROWS, this.heat.maxTextureSize), m.interval_ms) : null;
        if (this.store) this.heat.attach(this.store);
        this.fitTime();
      } else {
        this.loading = false;
        if (this.autoFit) this.fitTime(false);
        this.updateRange();
        this.updateMessage();
      }
      this.dirty();
      return;
    }
    if (m.sym !== this.symbol || m.tier !== this.tier) return;
    if (m.t === "lk") {
      this.bars = m.x.map(([t, o, h, l, c, v]) => ({ t, o, h, l, c, v }));
      this.barMs = intervalMs(m.iv);
      if (!this.store) this.fitTime();
      this.updateMessage();
      this.dirty();
      return;
    }
    // Spalte
    if (!this.store && this.hello && !this.hello.bucket_ticks) {
      // erste Spalte überhaupt (Orderbuch lief vorher nicht): jetzt Skalierung bekannt
      this.hello = { ...this.hello, bucket_ticks: m.bt };
      const cols = Math.ceil(this.hello.span_ms / this.hello.interval_ms) + 64;
      this.store = new HeatmapStore(Math.min(cols, this.heat.maxTextureSize), Math.min(ROWS, this.heat.maxTextureSize), this.hello.interval_ms);
      this.heat.attach(this.store);
    }
    const s = this.store;
    if (!s || !this.hello) return;
    const values = toFloat32(m.q);
    const idx = m.i ? toInt32(m.i) : null;
    const center = m.bb !== null && m.ba !== null ? Math.floor((m.bb + m.ba) / 2 / (m.bt * m.f)) : null;
    // Laufende Spalte (p) und danach die endgültige haben denselben Zeitstempel -> ersetzen
    const { index, replaced } = s.upsertColumn(m.ts, m.s, m.b, values, idx, center);
    if (index >= 0) {
      s.setBbo(m.ts, m.bb, m.ba);
      if (m.ll || m.lsh) this.liq.set(m.ts, [m.ll, m.lsh]);
      else this.liq.delete(m.ts);
      // Zurückgescrollt: betrachteten Zeitpunkt festhalten
      if (!replaced && this.view.offset > 0 && !this.loading) this.view = { ...this.view, offset: this.view.offset + 1 };
      if (!replaced && this.autoFit && !this.loading) this.fitTime(false);
    }
    if (!this.loading) this.updateMessage();
    this.dirty();
  }

  private inst(): Instrument | null {
    const h = this.hello;
    if (!h || !this.symbol) return null;
    const tick = h.tick_size ?? this.markets.bySymbol.get(this.symbol)?.tickSize?.toString() ?? null;
    if (!tick) return null;
    return {
      symbol: this.symbol,
      tickSize: Number(tick),
      tickDecimals: decimalsOf(String(tick)),
      bucketTicks: (h.bucket_ticks ?? 1) * h.factor,
    };
  }

  private latestTs(): number | null {
    if (this.store?.count) return this.store.latestTs;
    const last = this.bars[this.bars.length - 1];
    return last ? Math.floor((last.t + this.barMs) / (this.hello?.interval_ms ?? 60_000)) * (this.hello?.interval_ms ?? 60_000) : null;
  }

  private updateMessage(): void {
    const s = this.store;
    const name = this.symbol ? (this.markets.bySymbol.get(this.symbol)?.label ?? this.symbol) : "";
    if (this.loading) return;
    if (!s || s.count === 0) {
      this.showMessage(
        `Für ${name} gibt es noch keine Langzeit-Daten.\n` +
          "Das Orderbuch läuft jetzt – ab sofort wird gesammelt (auch nach Neustarts).\n" +
          "Kerzen sind sofort da; die Heatmap füllt sich mit der Zeit.",
      );
      this.info.textContent = "";
      return;
    }
    this.showMessage(null);
    const oldest = s.oldestTs!;
    const spanCovered = (s.latestTs! - oldest) / (this.hello?.span_ms ?? 1);
    this.info.textContent =
      `Heatmap seit ${dateLabel(oldest)} ${timeLabel(oldest, false, false)}` +
      (spanCovered < 0.98 ? ` (${Math.round(spanCovered * 100)} % des Zeitraums gesammelt)` : "");
  }

  private showMessage(text: string | null): void {
    this.message.hidden = text === null;
    if (text !== null) this.message.textContent = text;
  }

  // --- Ansicht -------------------------------------------------------------------------

  private size(): { pw: number; ph: number } {
    return {
      pw: Math.max(1, this.plot.clientWidth - AXIS_W - (this.layers.dom ? DOM_W : 0)),
      ph: Math.max(1, this.plot.clientHeight - AXIS_H - (this.layers.liqs ? LIQ_H : 0)),
    };
  }

  private resize(): void {
    if (!this.visible) return;
    this.dpr = window.devicePixelRatio || 1;
    const { pw, ph } = this.size();
    const lh = this.layers.liqs ? LIQ_H : 0;
    const place = (c: HTMLCanvasElement, x: number, y: number, w: number, h: number) => {
      c.style.left = `${x}px`;
      c.style.top = `${y}px`;
      c.style.width = `${w}px`;
      c.style.height = `${h}px`;
      c.width = Math.max(1, Math.round(w * this.dpr));
      c.height = Math.max(1, Math.round(h * this.dpr));
    };
    place(this.glCanvas, 0, 0, pw, ph);
    place(this.yAxis, pw, 0, AXIS_W, ph + lh + AXIS_H);
    this.domCanvas.style.display = this.layers.dom ? "block" : "none";
    if (this.layers.dom) place(this.domCanvas, pw + AXIS_W, 0, DOM_W, ph + lh + AXIS_H);
    this.liqCanvas.style.display = lh ? "block" : "none";
    if (lh) place(this.liqCanvas, 0, ph, pw, lh);
    place(this.xAxis, 0, ph + lh, pw, AXIS_H);
    this.crossV.style.height = `${ph + lh}px`;
    this.crossH.style.width = `${pw}px`;
    if (this.view.offset === 0 && this.autoY) this.fitTime();
    this.dirty();
  }

  /**
   * Zeitraum einpassen: der ganze Zeitraum der Stufe – solange die Heatmap noch nicht so
   * weit zurückreicht, nur der gesammelte Teil (mindestens 1 h bzw. 24 Spalten). Sonst
   * wäre beim ersten Start nur ein schmaler Streifen am rechten Rand zu sehen.
   */
  private fitTime(resetY = true): void {
    const h = this.hello;
    if (!h) return;
    const { pw } = this.size();
    const s = this.store;
    let span = h.span_ms;
    if (s && s.count) {
      const collected = s.latestTs! - s.oldestTs! + h.interval_ms;
      span = Math.min(h.span_ms, Math.max(collected, MIN_FIT_MS, MIN_FIT_COLS * h.interval_ms));
    }
    this.view = { ...this.view, colWidth: pw / Math.max(1, span / h.interval_ms), offset: 0 };
    if (resetY) this.autoY = true;
    this.autoFit = true;
  }

  /** Preisbereich an die sichtbaren Kerzen (sonst BBO) anpassen. */
  private fitY(): void {
    const inst = this.inst();
    const h = this.hello;
    const latest = this.latestTs();
    if (!inst || !h || latest === null) return;
    const { pw } = this.size();
    const tEnd = latest - this.view.offset * h.interval_ms;
    const tStart = tEnd - (pw / this.view.colWidth) * h.interval_ms;
    let lo = Infinity;
    let hi = -Infinity;
    for (const b of this.bars) {
      if (b.t + this.barMs < tStart || b.t > tEnd) continue;
      lo = Math.min(lo, b.l);
      hi = Math.max(hi, b.h);
    }
    if (!Number.isFinite(lo) && this.store) {
      const s = this.store;
      for (let k = Math.max(0, Math.floor(this.view.offset)); k < s.count; k++) {
        const i = s.indexAgo(k);
        if (!Number.isNaN(s.bestBid[i]!)) {
          lo = Math.min(lo, s.bestBid[i]! * inst.tickSize);
          hi = Math.max(hi, s.bestAsk[i]! * inst.tickSize);
        }
      }
    }
    if (!Number.isFinite(lo) || hi <= lo) return;
    const pad = (hi - lo) * 0.08;
    const u = (p: number) => p / inst.tickSize / inst.bucketTicks;
    this.view = { ...this.view, yLo: u(lo - pad), yHi: u(hi + pad) };
  }

  private updateRange(): void {
    const s = this.store;
    if (!this.visible || !s || s.count === 0) return;
    const { pw } = this.size();
    const k0 = Math.max(0, Math.floor(this.view.offset));
    const n = Math.min(visibleCols(this.view, pw), s.count - k0);
    const out: number[] = [];
    const colStride = Math.max(1, Math.floor(n / 200));
    const r0 = Math.max(0, Math.floor(this.view.yLo) - s.origin);
    const r1 = Math.min(s.rows, Math.ceil(this.view.yHi) - s.origin);
    const rowStride = Math.max(1, Math.floor((r1 - r0) / 200));
    for (let k = k0; k < k0 + n; k += colStride) {
      const base = s.indexAgo(k) * s.rows;
      for (let r = r0; r < r1; r += rowStride) {
        const v = s.data[base + r]!;
        if (v > 0) out.push(v);
      }
    }
    const r = computeRange(Float32Array.from(out), this.scale());
    if (r) {
      this.range = r;
      this.dirty();
    }
  }

  private maxOffset(): number {
    const h = this.hello;
    const latest = this.latestTs();
    if (!h || latest === null) return 0;
    let oldest = this.store?.oldestTs ?? latest;
    if (this.bars[0]) oldest = Math.min(oldest, this.bars[0].t);
    return Math.max(0, Math.floor((latest - oldest) / h.interval_ms));
  }

  private bindInput(): void {
    const el = this.plot;
    const local = (e: MouseEvent) => {
      const r = el.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    };
    el.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        const f = Math.pow(1.0015, e.deltaY);
        const { ph } = this.size();
        const { y } = local(e);
        if (e.shiftKey || y > ph) {
          this.view = zoomX(this.view, 1 / f);
          this.autoFit = false;
        } else {
          this.view = zoomY(this.view, f, yToUnit(y, this.view, ph));
          this.autoY = false;
        }
        this.dirty();
      },
      { passive: false },
    );
    el.addEventListener("pointerdown", (e) => {
      this.drag = { x: e.clientX, y: e.clientY, view: this.view };
      el.setPointerCapture(e.pointerId);
    });
    el.addEventListener("pointermove", (e) => {
      this.mouse = local(e);
      if (this.drag) {
        const { ph } = this.size();
        const d = this.drag;
        const dy = e.clientY - d.y;
        let v = d.view;
        if (Math.abs(dy) > 2) {
          v = panY(v, (dy * (v.yHi - v.yLo)) / ph);
          this.autoY = false;
        }
        const offset = Math.min(Math.max(d.view.offset + (e.clientX - d.x) / d.view.colWidth, 0), this.maxOffset());
        if (offset !== d.view.offset) this.autoFit = false;
        this.view = { ...v, offset };
        this.dirty();
      }
      this.hover();
    });
    el.addEventListener("pointerup", () => (this.drag = null));
    el.addEventListener("pointerleave", () => {
      this.mouse = null;
      this.hover();
    });
    el.addEventListener("dblclick", () => {
      this.fitTime();
      this.dirty();
    });
  }

  // --- Zeichnen ------------------------------------------------------------------------

  private render(): void {
    const inst = this.inst();
    const h = this.hello;
    const { pw, ph } = this.size();
    if (this.autoY) this.fitY();
    const px = this.dpr;
    const s = this.store;
    this.heat.upload();
    this.heat.draw(this.view, this.layers.heat ? this.range : null, px, true);
    const latest = this.latestTs();
    if (inst && h && s && s.count) {
      const p: SceneParams = {
        store: s,
        view: this.view,
        pw,
        ph,
        bucketTicks: inst.bucketTicks,
        tickSize: inst.tickSize,
        gridStepTicks: gridStepTicks(this.view, ph, inst),
        bubblePxPerSqrtKusd: 1,
        bubbleMaxPx: 1,
      };
      buildGrid(p, this.vGrid);
      this.prims.draw(PRIM.SOFT_LINE, this.vGrid.data, this.vGrid.n, pw, ph, px, LINE.grid.half, LINE.grid.feather);
      if (!this.layers.candles) {
        buildBbo(p, this.vBbo);
        this.prims.draw(PRIM.SOFT_LINE, this.vBbo.data, this.vBbo.n, pw, ph, px, LINE.bbo.half, LINE.bbo.feather);
      }
    }
    if (inst && h && this.layers.candles && this.bars.length && latest !== null) {
      // Kerzen brauchen nur die Zeitachse; ohne Heatmap-Daten eine leere Hilfs-Spalte
      const store = s && s.count ? s : this.axisStore(latest, h.interval_ms);
      buildCandles(
        this.bars,
        this.barMs / 60_000,
        { store, view: this.view, pw, ph, intervalMs: h.interval_ms, tickSize: inst.tickSize, bucketTicks: inst.bucketTicks },
        this.vCandles,
      );
      this.rrects.draw(this.vCandles.data, this.vCandles.n, pw, ph, px);
    }
    drawPriceAxis(this.yAxis, this.dpr, this.layers.candles ? null : s, this.view, inst, ph);
    if (this.layers.dom) {
      const data = s && inst && h && s.count ? buildDomRows(s, this.view, pw, ph, inst.bucketTicks, h.factor) : null;
      drawDom(this.domCanvas, this.dpr, data, this.view, ph, inst?.bucketTicks ?? 1, {
        usd: this.domUsd(),
        tickSize: inst?.tickSize ?? 1,
        average: true,
      });
    }
    drawTimeAxis(this.xAxis, this.dpr, latest, this.view, h?.interval_ms ?? 60_000, pw, true);
    if (this.layers.liqs) this.drawLiqs(pw, latest);
    this.hover();
  }

  private axisStore(latest: number, iv: number): HeatmapStore {
    const tmp = new HeatmapStore(1, 1, iv);
    tmp.pushColumn(latest, false, 0, new Float32Array(0));
    return tmp;
  }

  /** Liquidationen je Spalte: Longs nach oben (orange), Shorts nach unten (blau). */
  private drawLiqs(pw: number, latest: number | null): void {
    const c = this.liqCanvas;
    const ctx = c.getContext("2d")!;
    const dpr = this.dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#232427";
    ctx.fillRect(0, 0, pw, LIQ_H);
    ctx.fillStyle = "#34363b";
    ctx.fillRect(0, 0, pw, 1);
    const h = this.hello;
    const mid = LIQ_H / 2 + 6;
    ctx.fillStyle = "rgba(255,255,255,0.06)";
    ctx.fillRect(0, mid, pw, 1);
    ctx.font = "10px ui-monospace, monospace";
    ctx.fillStyle = "#80848b";
    ctx.textBaseline = "top";
    ctx.fillText("Liquidationen je Spalte (Longs ▲ · Shorts ▼) · Volumen je Kerze (grau)", 8, 4);
    if (!h || latest === null) return;
    this.drawVolume(ctx, pw, latest, h.interval_ms);
    if (!this.liq.size) return;
    // Pro Pixel-Spalte aufsummieren (bei 1M liegen mehrere Spalten auf einem Pixel)
    const bins = new Map<number, [number, number]>();
    for (const [ts, [ll, ls]] of this.liq) {
      const k = (latest - ts) / h.interval_ms;
      const x = Math.floor(colToX(k, this.view, pw));
      if (x < -2 || x > pw + 2) continue;
      const b = bins.get(x) ?? [0, 0];
      b[0] += ll;
      b[1] += ls;
      bins.set(x, b);
    }
    let max = 0;
    for (const [a, b] of bins.values()) max = Math.max(max, a, b);
    if (!max) return;
    const w = Math.max(1, Math.floor(this.view.colWidth * 0.8));
    const room = mid - 18;
    const roomDown = LIQ_H - mid - 4;
    for (const [x, [ll, ls]] of bins) {
      if (ll) {
        ctx.fillStyle = "rgba(255,168,51,0.85)";
        const hh = Math.max(1, (ll / max) * room);
        ctx.fillRect(x - w / 2, mid - hh, w, hh);
      }
      if (ls) {
        ctx.fillStyle = "rgba(90,204,255,0.85)";
        ctx.fillRect(x - w / 2, mid + 1, w, Math.max(1, (ls / max) * roomDown));
      }
    }
    ctx.fillStyle = "#80848b";
    ctx.textBaseline = "top";
    const label = `max ${fmtUsd(max)}`;
    ctx.fillText(label, pw - ctx.measureText(label).width - 6, 4);
  }

  /** Gehandeltes Volumen je Kerze als blasse Balken über die ganze Höhe des Bereichs.
   *  Kommt mit den Kerzen von der Börse – auch für die Zeit vor dem ersten Start. */
  private drawVolume(ctx: CanvasRenderingContext2D, pw: number, latest: number, iv: number): void {
    let max = 0;
    const spans: [number, number, number][] = [];
    for (const b of this.bars) {
      const x0 = colToX((latest - b.t) / iv, this.view, pw);
      const x1 = colToX((latest - b.t - this.barMs) / iv, this.view, pw);
      if (x1 < 0 || x0 > pw) continue;
      spans.push([x0, x1, b.v]);
      max = Math.max(max, b.v);
    }
    if (!max) return;
    ctx.fillStyle = "rgba(200, 204, 212, 0.13)";
    const top = 18;
    for (const [x0, x1, v] of spans) {
      const hh = (v / max) * (LIQ_H - top - 2);
      ctx.fillRect(Math.min(x0, x1) + 0.5, LIQ_H - 2 - hh, Math.max(1, Math.abs(x1 - x0) - 1), hh);
    }
  }

  /** Fadenkreuz + Tooltip (Zeit, Preis, Liquidität, Kerze, Liquidationen). */
  private hover(): void {
    const { pw, ph } = this.size();
    const m = this.mouse;
    const lh = this.layers.liqs ? LIQ_H : 0;
    const inside = m !== null && m.x >= 0 && m.x < pw && m.y >= 0 && m.y < ph + lh;
    for (const e of [this.crossV, this.crossH, this.tooltip]) e.style.display = inside ? "block" : "none";
    if (!inside || !m) return;
    this.crossV.style.transform = `translate(${Math.round(m.x)}px, 0)`;
    this.crossH.style.display = m.y < ph ? "block" : "none";
    this.crossH.style.transform = `translate(0, ${Math.round(m.y)}px)`;
    const h = this.hello;
    const inst = this.inst();
    const latest = this.latestTs();
    if (!h || !inst || latest === null) {
      this.tooltip.style.display = "none";
      return;
    }
    const k = xToCol(m.x, this.view, pw);
    const t = latest - k * h.interval_ms;
    const lines = [`${dateLabel(t)} ${timeLabel(t, false, false)}`];
    if (m.y < ph) {
      const unit = yToUnit(m.y, this.view, ph);
      lines.push(formatPrice(Math.round(unit * inst.bucketTicks), inst));
      if (this.store && k < this.store.count) {
        const v = this.store.valueAt(k, Math.floor(unit));
        const step = formatPrice(inst.bucketTicks, { ...inst, tickDecimals: inst.tickDecimals });
        lines.push(`Liquidität Ø: ${v.toLocaleString("de-DE", { maximumFractionDigits: 2 })} je ${step}-Stufe`);
      }
    }
    const bar = this.bars.find((b) => b.t <= t && t < b.t + this.barMs);
    if (bar) {
      const f = (v: number) => v.toLocaleString("de-DE", { maximumFractionDigits: inst.tickDecimals });
      lines.push(`O ${f(bar.o)}  H ${f(bar.h)}  L ${f(bar.l)}  C ${f(bar.c)}`);
    }
    const colTs = Math.ceil(t / h.interval_ms) * h.interval_ms;
    const lq = this.liq.get(colTs);
    if (lq) lines.push(`Liq. Longs ${fmtUsd(lq[0])} · Shorts ${fmtUsd(lq[1])}`);
    this.tooltip.textContent = lines.join("\n");
    const tw = this.tooltip.offsetWidth;
    const th = this.tooltip.offsetHeight;
    const tx = m.x + 14 + tw > pw ? m.x - tw - 14 : m.x + 14;
    const ty = Math.min(Math.max(m.y - th / 2, 4), ph + lh - th - 4);
    this.tooltip.style.transform = `translate(${Math.round(tx)}px, ${Math.round(ty)}px)`;
  }
}
