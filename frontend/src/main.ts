import "./style.css";
import { AXIS_H, AXIS_W, drawPriceAxis, drawTimeAxis, formatPrice, gridStepTicks, timeLabel, type Instrument } from "./axes";
import { aggregate, buildCandles, CandleSeries, RRectBuilder, type CandleMinutes } from "./candles";
import { computeRange, type ScaleRange, type ScaleSettings } from "./colorscale";
import { CYCLE_PRESETS } from "./cycles";
import { loadFeatures, saveFeatures } from "./features";
import { HeatmapRenderer, PRIM, PrimRenderer, RRectRenderer } from "./gl";
import { Markets, decimalsOf } from "./markets";
import { connect, toFloat32, type Hello } from "./net";
import {
  buildBbo,
  buildBubbles,
  buildGrid,
  buildLiquidations,
  buildUnsynced,
  LINE,
  VertexBuilder,
  type SceneParams,
} from "./scene";
import { Sidebar, type CycleChoice } from "./sidebar";
import { HeatmapStore, rowsFor, type TradeBin } from "./store";
import { panY, priceUnit, recenter, visibleCols, xToCol, yToUnit, zoomX, zoomY, type View } from "./viewport";

const WS_URL = (import.meta.env.VITE_COCKBOOK_WS as string | undefined) ?? "ws://127.0.0.1:8765/ws";
const SETTINGS_KEY = "cockbook.settings.v1";
const SYMBOL_KEY = "cockbook.symbol.v1";
const CANDLE_KEY = "cockbook.candles.v1";
const CYCLE_KEY = "cockbook.cycle.v1";
const MAX_HISTORY_COLS = 4096;

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const chartEl = $<HTMLElement>("chart");
const glCanvas = $<HTMLCanvasElement>("gl");
const yAxisCanvas = $<HTMLCanvasElement>("yaxis");
const xAxisCanvas = $<HTMLCanvasElement>("xaxis");
const crossV = $<HTMLElement>("crossV");
const crossH = $<HTMLElement>("crossH");
const tooltipEl = $<HTMLElement>("tooltip");
const contrastEl = $<HTMLInputElement>("contrast");
const thresholdEl = $<HTMLInputElement>("threshold");
const perfEl = $<HTMLElement>("perf");

const heatmap = new HeatmapRenderer(glCanvas);
const prims = new PrimRenderer(heatmap.gl);
const rrects = new RRectRenderer(heatmap.gl);
const params = new URLSearchParams(location.search);

/**
 * Render-Skala der WebGL-Fläche (interne Auflösung relativ zur Anzeige). Mit GPU 1.0;
 * auf Software-Renderern (z. B. VM ohne GPU-Durchreichung) 0.5 – dort kostet jedes
 * Pixel CPU-Zeit. Override: ?scale=0.75
 */
const renderScale = Number(params.get("scale")) || (heatmap.isSoftware ? 0.5 : 1);
/** Jeden Frame zeichnen statt nur bei Änderungen (für Messungen): ?continuous / ?bench */
const continuous = params.has("continuous") || params.has("bench");

// --- Zustand -----------------------------------------------------------------------------

let currentSymbol: string | null = null;
try {
  currentSymbol = localStorage.getItem(SYMBOL_KEY);
} catch {
  /* egal */
}
let hello: Hello | null = null;
let inst: Instrument | null = null;
let store: HeatmapStore | null = null;
let view: View = { yLo: 0, yHi: 100, colWidth: 2, offset: 0, autoCenter: true };
let viewInitialized = false;
let range: ScaleRange | null = null;
let dpr = window.devicePixelRatio || 1;
let mouse: { x: number; y: number } | null = null;
let columnsReceived = 0;
let dataVersion = 0;
let sizeVersion = 0;
let featureVersion = 0;

const markets = new Markets();
const features = loadFeatures();
const candles = new CandleSeries();

function loadJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    if (raw) return { ...fallback, ...(JSON.parse(raw) as T) };
  } catch {
    /* egal */
  }
  return fallback;
}
function saveJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* egal */
  }
}
let candleMinutes: CandleMinutes = loadJson(CANDLE_KEY, { minutes: 1 as CandleMinutes }).minutes;
const defaultCycle = CYCLE_PRESETS[0]!;
let cycleChoice: CycleChoice = loadJson(CYCLE_KEY, {
  id: defaultCycle.id,
  start: defaultCycle.start,
  end: defaultCycle.end,
});

interface Saved {
  contrastLog: number;
  threshold: number;
  colWidth: number;
}
const DEFAULTS: Saved = { contrastLog: 0, threshold: 0.1, colWidth: 2 };
function loadSettings(): Saved {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (raw) return { ...DEFAULTS, ...(JSON.parse(raw) as Partial<Saved>) };
  } catch {
    /* privates Fenster o. Ä. */
  }
  return DEFAULTS;
}
function saveSettings(): void {
  try {
    const s: Saved = {
      contrastLog: Number(contrastEl.value),
      threshold: Number(thresholdEl.value),
      colWidth: view.colWidth,
    };
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
  } catch {
    /* egal */
  }
}
const saved = loadSettings();
contrastEl.value = String(saved.contrastLog);
thresholdEl.value = String(saved.threshold);
view.colWidth = saved.colWidth;

function scaleSettings(): ScaleSettings {
  // Regler logarithmisch: -2..3 -> Kontrast 0.25..8
  return { contrast: Math.pow(2, Number(contrastEl.value)), threshold: Number(thresholdEl.value) };
}

// --- Layout ------------------------------------------------------------------------------

function plotSize(): { pw: number; ph: number } {
  return { pw: Math.max(1, chartEl.clientWidth - AXIS_W), ph: Math.max(1, chartEl.clientHeight - AXIS_H) };
}

function place(c: HTMLCanvasElement, x: number, y: number, w: number, h: number, scale: number): void {
  c.style.left = `${x}px`;
  c.style.top = `${y}px`;
  c.style.width = `${w}px`;
  c.style.height = `${h}px`;
  c.width = Math.max(1, Math.round(w * scale));
  c.height = Math.max(1, Math.round(h * scale));
}

function resize(): void {
  dpr = window.devicePixelRatio || 1;
  const { pw, ph } = plotSize();
  place(glCanvas, 0, 0, pw, ph, dpr * renderScale);
  place(yAxisCanvas, pw, 0, AXIS_W, ph + AXIS_H, dpr);
  place(xAxisCanvas, 0, ph, pw, AXIS_H, dpr);
  crossV.style.height = `${ph}px`;
  crossH.style.width = `${pw}px`;
  sizeVersion++;
}
new ResizeObserver(resize).observe(chartEl);
resize();

// --- Seitenleiste ----------------------------------------------------------------------------

const sidebar = new Sidebar(
  $<HTMLElement>("sidebar"),
  markets,
  features,
  {
    onSelect: (sym) => selectSymbol(sym),
    onToggle: (id, on) => {
      saveFeatures(features);
      featureVersion++;
      if (id === "candles" && on) fitToCandles();
    },
    onCandleInterval: (m) => {
      candleMinutes = m;
      saveJson(CANDLE_KEY, { minutes: m });
      featureVersion++;
      fitToCandles();
    },
    onCycle: (c) => {
      cycleChoice = c;
      saveJson(CYCLE_KEY, c);
    },
  },
  candleMinutes,
  cycleChoice,
);
setInterval(() => sidebar.update(Date.now()), 500);

function setBadge(id: string, text: string, cls: string): void {
  const el = $<HTMLElement>(id);
  el.textContent = text;
  el.className = `badge ${cls}`;
}

function selectSymbol(sym: string): void {
  if (sym === currentSymbol && store) return;
  currentSymbol = sym;
  try {
    localStorage.setItem(SYMBOL_KEY, sym);
  } catch {
    /* egal */
  }
  // Chart leeren, bis hello + Historie des neuen Symbols da sind
  hello = null;
  inst = null;
  store = null;
  range = null;
  candles.clear();
  viewInitialized = false;
  view = { ...view, offset: 0, autoCenter: true };
  dataVersion++;
  sidebar.setActive(sym);
  $<HTMLElement>("symbol").textContent = sym;
  setBadge("sync", "lade …", "");
  conn.subscribe(sym);
}

// --- Daten -------------------------------------------------------------------------------

const conn = connect(
  WS_URL,
  {
    onConnection(ok) {
      if (!ok) setBadge("sync", "keine Verbindung", "off");
    },
    onSymbols(s) {
      for (const info of s.symbols) markets.ensure(info.symbol, info.tick_size);
      const list = s.symbols.map((x) => x.symbol);
      sidebar.setSymbols(list);
      if (!currentSymbol || !list.includes(currentSymbol)) {
        if (list[0]) selectSymbol(list[0]);
      } else {
        sidebar.setActive(currentSymbol);
      }
    },
    onHello(h) {
      if (currentSymbol && h.symbol !== currentSymbol) return; // verspätet, altes Symbol
      currentSymbol = h.symbol;
      const key = (x: Hello | null) =>
        x && [x.exchange, x.symbol, x.tick_size, x.bucket_ticks, x.interval_ms, x.half_range_buckets].join("|");
      const changed = key(h) !== key(hello);
      hello = h;
      inst = {
        symbol: h.symbol,
        tickSize: Number(h.tick_size),
        tickDecimals: decimalsOf(h.tick_size),
        bucketTicks: h.bucket_ticks,
      };
      markets.ensure(h.symbol, h.tick_size);
      $<HTMLElement>("symbol").textContent = `${h.exchange} · ${h.symbol}`;
      setBadge("mode", h.mode.toUpperCase(), h.mode);
      sidebar.setActive(h.symbol);
      if (changed || !store) {
        const maxTex = heatmap.maxTextureSize;
        store = new HeatmapStore(Math.min(MAX_HISTORY_COLS, maxTex), rowsFor(h.half_range_buckets, maxTex));
        heatmap.attach(store);
        viewInitialized = false;
        range = null;
      }
    },
    onStatus(s) {
      if (s.symbol !== currentSymbol) return;
      setBadge("sync", s.synced ? "SYNC" : `NICHT SYNCHRON (${s.reason})`, s.synced ? "ok" : "off");
    },
    onStat(f) {
      markets.apply(f);
    },
    onChart(f) {
      if (f.sym !== currentSymbol) return;
      if (f.t === "kh" || f.t === "k") {
        if (f.t === "kh") candles.setHistory(f.x);
        else {
          const [t, o, h, l, c, v] = f.x;
          candles.upsert({ t, o, h, l, c, v });
        }
        dataVersion++;
        return;
      }
      if (!store || !hello) return;
      dataVersion++;
      if (f.t === "col") {
        const values = toFloat32(f.q);
        if (store.pushColumn(f.ts, f.s, f.b, values) < 0) return; // schon vorhanden
        columnsReceived++;
        if (!viewInitialized && f.s && values.length) {
          const center = f.b + values.length / 2;
          const half = hello.half_range_buckets * 0.6;
          view = { ...view, yLo: center - half, yHi: center + half };
          viewInitialized = true;
        }
        // Beim Zurückscrollen bleibt der betrachtete Zeitpunkt stehen
        if (view.offset > 0) view = { ...view, offset: Math.min(view.offset + 1, store.count - 1) };
      } else if (f.t === "bbo") {
        store.setBbo(f.ts, f.bb, f.ba);
        if (view.autoCenter && view.offset === 0 && f.bb !== null && f.ba !== null) {
          view = recenter(view, priceUnit((f.bb + f.ba) / 2, hello.bucket_ticks), hello.ui.recenter_margin);
        }
      } else if (f.t === "tr") {
        store.setTrades(
          f.ts,
          f.x.map(([side, vwapTicks, qty, count]): TradeBin => ({ side: side > 0 ? 1 : -1, vwapTicks, qty, count })),
        );
      } else if (f.t === "lq") {
        store.setLiquidations(
          f.ts,
          f.x.map(([side, priceTicks, qty]) => ({ side: side > 0 ? 1 : -1, priceTicks, qty })),
        );
      }
    },
  },
  () => currentSymbol,
);

// --- Farbskala (alle 500 ms aus dem sichtbaren Bereich) -----------------------------------

function updateRange(): void {
  if (!store || store.count === 0) return;
  const { pw } = plotSize();
  const n = Math.min(visibleCols(view, pw), store.count - view.offset);
  const colStride = Math.max(1, Math.floor(n / 200));
  const r0 = Math.max(0, Math.floor(view.yLo) - store.origin);
  const r1 = Math.min(store.rows, Math.ceil(view.yHi) - store.origin);
  const rowStride = Math.max(1, Math.floor((r1 - r0) / 200));
  const out: number[] = [];
  for (let k = view.offset; k < view.offset + n; k += colStride) {
    const base = store.indexAgo(k) * store.rows;
    for (let r = r0; r < r1; r += rowStride) {
      const v = store.data[base + r]!;
      if (v > 0) out.push(v);
    }
  }
  range = computeRange(Float32Array.from(out), scaleSettings()) ?? range;
}
setInterval(updateRange, 500);
for (const el of [contrastEl, thresholdEl]) {
  el.addEventListener("input", () => {
    updateRange();
    saveSettings();
  });
}

// --- Interaktion -------------------------------------------------------------------------

function goLive(): void {
  view = { ...view, offset: 0, autoCenter: true };
  if (store && hello && store.count) {
    const i = store.latest;
    const bb = store.bestBid[i]!;
    const ba = store.bestAsk[i]!;
    if (!Number.isNaN(bb) && !Number.isNaN(ba)) {
      view = recenter(view, priceUnit((bb + ba) / 2, hello.bucket_ticks), 0, true);
    }
  }
}

function localPos(e: MouseEvent): { x: number; y: number } {
  const rect = chartEl.getBoundingClientRect();
  return { x: e.clientX - rect.left, y: e.clientY - rect.top };
}

chartEl.addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    const factor = Math.pow(1.0015, e.deltaY);
    const { ph } = plotSize();
    const { y } = localPos(e);
    if (e.shiftKey || y > ph) {
      view = zoomX(view, 1 / factor);
      saveSettings();
      if (features.candles && view.autoCenter) fitToCandles();
    } else {
      view = zoomY(view, factor, yToUnit(y, view, ph));
    }
    updateHover();
  },
  { passive: false },
);

let drag: { x: number; y: number; view: View } | null = null;
chartEl.addEventListener("pointerdown", (e) => {
  drag = { x: e.clientX, y: e.clientY, view };
  chartEl.setPointerCapture(e.pointerId);
});
chartEl.addEventListener("pointermove", (e) => {
  mouse = localPos(e);
  if (drag && store) {
    const { ph } = plotSize();
    const dy = e.clientY - drag.y;
    const dx = e.clientX - drag.x;
    const unitsPerPx = (drag.view.yHi - drag.view.yLo) / ph;
    let v = Math.abs(dy) > 2 ? panY(drag.view, dy * unitsPerPx) : { ...drag.view };
    const offset = Math.round(drag.view.offset + dx / drag.view.colWidth);
    v = { ...v, offset: Math.min(Math.max(offset, 0), maxOffset()) };
    if (v.offset > 0) v.autoCenter = false;
    view = v;
  }
  updateHover();
});
chartEl.addEventListener("pointerup", () => (drag = null));
chartEl.addEventListener("pointerleave", () => {
  mouse = null;
  updateHover();
});
chartEl.addEventListener("dblclick", goLive);
$<HTMLButtonElement>("live").addEventListener("click", goLive);
window.addEventListener("keydown", (e) => {
  if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return;
  if (e.key === "c" || e.key === "C") goLive();
  if (e.key === "f" || e.key === "F") fitToCandles();
});

/**
 * Preisbereich an die sichtbaren Kerzen anpassen (mit etwas Rand). Ohne das ragen
 * Kerzen beim Herauszoomen in der Zeit schnell aus dem Bild, weil der Preisbereich
 * für die Heatmap (± wenige Promille) gemacht ist.
 */
function fitToCandles(): void {
  if (!store || !inst || !hello || store.count === 0) return;
  const { pw } = plotSize();
  const latest = store.ts[store.latest]!;
  const tEnd = latest + view.offset * hello.interval_ms;
  const tStart = tEnd - (pw / view.colWidth) * hello.interval_ms;
  const p = candleMinutes * 60_000;
  let lo = Infinity;
  let hi = -Infinity;
  for (const b of aggregate(candles.all(), candleMinutes)) {
    if (b.t + p < tStart || b.t > tEnd) continue;
    lo = Math.min(lo, b.l);
    hi = Math.max(hi, b.h);
  }
  if (!Number.isFinite(lo) || hi <= lo) return;
  const pad = (hi - lo) * 0.08;
  const toUnit = (price: number) => price / inst!.tickSize / inst!.bucketTicks;
  view = { ...view, yLo: toUnit(lo - pad), yHi: toUnit(hi + pad) };
}

/** Wie weit man zurückscrollen kann: Heatmap-Historie oder – mit Kerzen – deren Historie. */
function maxOffset(): number {
  if (!store || store.count === 0) return 0;
  let cols = store.count - 1;
  const first = candles.first;
  if (features.candles && first !== null && hello) {
    cols = Math.max(cols, Math.floor((store.ts[store.latest]! - first) / hello.interval_ms));
  }
  return cols;
}

/** Fadenkreuz + Tooltip: nur DOM-Updates, kein Neuzeichnen des Plots. */
function updateHover(): void {
  const { pw, ph } = plotSize();
  const inside = mouse !== null && mouse.x >= 0 && mouse.x < pw && mouse.y >= 0 && mouse.y < ph;
  for (const el of [crossV, crossH, tooltipEl]) el.style.display = inside ? "block" : "none";
  if (!inside || !mouse) return;
  crossV.style.transform = `translate(${Math.round(mouse.x)}px, 0)`;
  crossH.style.transform = `translate(0, ${Math.round(mouse.y)}px)`;
  if (!store || !inst || store.count === 0) {
    tooltipEl.style.display = "none";
    return;
  }
  const k = xToCol(mouse.x, view, pw);
  if (k >= store.count) {
    tooltipEl.style.display = "none";
    return;
  }
  const bucket = Math.floor(yToUnit(mouse.y, view, ph));
  const qty = store.valueAt(k, bucket);
  const lo = bucket * inst.bucketTicks;
  const hi = lo + inst.bucketTicks - 1;
  const price = inst.bucketTicks === 1 ? formatPrice(lo, inst) : `${formatPrice(lo, inst)} – ${formatPrice(hi, inst)}`;
  tooltipEl.textContent = [
    price,
    `Liquidität: ${qty.toLocaleString("de-DE", { maximumFractionDigits: 3 })}`,
    timeLabel(store.ts[store.indexAgo(k)]!, true),
  ].join("\n");
  const tw = tooltipEl.offsetWidth;
  const th = tooltipEl.offsetHeight;
  const tx = mouse.x + 14 + tw > pw ? mouse.x - tw - 14 : mouse.x + 14;
  const ty = Math.min(Math.max(mouse.y - th / 2, 4), ph - th - 4);
  tooltipEl.style.transform = `translate(${Math.round(tx)}px, ${Math.round(ty)}px)`;
}

// --- Render-Loop ---------------------------------------------------------------------------

const vUnsynced = new VertexBuilder(1024);
const vGrid = new VertexBuilder(1024);
const vBbo = new VertexBuilder(16384);
const vBubbles = new VertexBuilder(4096);
const vLiqs = new VertexBuilder(256);
const vCandles = new RRectBuilder();

function render(): void {
  const { pw, ph } = plotSize();
  const px = dpr * renderScale;
  heatmap.upload();
  heatmap.draw(view, features.heatmap ? range : null, px, features.smooth);
  if (store && inst && store.count > 0) {
    const p: SceneParams = {
      store,
      view,
      pw,
      ph,
      bucketTicks: inst.bucketTicks,
      tickSize: inst.tickSize,
      gridStepTicks: gridStepTicks(view, ph, inst),
      bubblePxPerSqrtKusd: hello?.ui.bubble_px_per_sqrt_kusd ?? 1.2,
      bubbleMaxPx: hello?.ui.bubble_max_px ?? 40,
    };
    buildUnsynced(p, vUnsynced);
    prims.draw(PRIM.FLAT, vUnsynced.data, vUnsynced.n, pw, ph, px);
    if (features.grid) {
      buildGrid(p, vGrid);
      prims.draw(PRIM.SOFT_LINE, vGrid.data, vGrid.n, pw, ph, px, LINE.grid.half, LINE.grid.feather);
    }
    if (features.candles && hello) {
      const bars = aggregate(candles.all(), candleMinutes);
      buildCandles(
        bars,
        candleMinutes,
        { store, view, pw, ph, intervalMs: hello.interval_ms, tickSize: inst.tickSize, bucketTicks: inst.bucketTicks },
        vCandles,
      );
      rrects.draw(vCandles.data, vCandles.n, pw, ph, px);
    }
    if (features.bbo) {
      buildBbo(p, vBbo);
      prims.draw(PRIM.SOFT_LINE, vBbo.data, vBbo.n, pw, ph, px, LINE.bbo.half, LINE.bbo.feather);
    }
    if (features.bubbles) {
      buildBubbles(p, vBubbles);
      prims.draw(PRIM.CIRCLE, vBubbles.data, vBubbles.n, pw, ph, px);
    }
    if (features.liqMarkers) {
      buildLiquidations(p, vLiqs);
      prims.draw(PRIM.DIAMOND, vLiqs.data, vLiqs.n, pw, ph, px);
    }
  }
  drawPriceAxis(yAxisCanvas, dpr, store, view, inst, ph);
  drawTimeAxis(xAxisCanvas, dpr, store, view, hello?.interval_ms ?? 100, pw);
}

let lastSig: unknown[] = [];
/** Neu zeichnen nur, wenn sich etwas geändert hat (Daten, Sicht, Skala, Größe, Schalter). */
function changed(): boolean {
  const sig = [dataVersion, view, range, sizeVersion, hello, featureVersion];
  const diff = sig.some((v, i) => v !== lastSig[i]);
  lastSig = sig;
  return diff;
}

// --- Performance-Messung ------------------------------------------------------------------

const frameTimes: number[] = []; // Abstand zwischen gezeichneten Frames
const workTimes: number[] = []; // JS-Zeit pro gezeichnetem Frame
let lastFrame = performance.now();
let frames = 0;

function frame(now: number): void {
  requestAnimationFrame(frame);
  if (!continuous && !changed()) return;
  const t0 = performance.now();
  render();
  if (mouse) updateHover(); // Tooltip-Wert ändert sich mit neuen Spalten
  workTimes.push(performance.now() - t0);
  frameTimes.push(now - lastFrame);
  lastFrame = now;
  frames++;
  if (frameTimes.length > 120) frameTimes.shift();
  if (workTimes.length > 120) workTimes.shift();
}
requestAnimationFrame(frame);

declare global {
  interface Window {
    __cockbookPerf?: {
      fps: number;
      workAvgMs: number;
      workP95Ms: number;
      columns: number;
      frames: number;
      renderScale: number;
      renderer: string;
      symbol: string | null;
    };
    __cockbookBench?: (n: number) => { renderMs: number; pixels: number };
  }
}

function stats(a: number[]): { avg: number; p95: number } {
  if (!a.length) return { avg: 0, p95: 0 };
  const s = [...a].sort((x, y) => x - y);
  return { avg: a.reduce((x, y) => x + y, 0) / a.length, p95: s[Math.floor(s.length * 0.95)]! };
}

setInterval(() => {
  const f = stats(frameTimes);
  const w = stats(workTimes);
  const fps = f.avg ? 1000 / f.avg : 0;
  window.__cockbookPerf = {
    fps,
    workAvgMs: w.avg,
    workP95Ms: w.p95,
    columns: columnsReceived,
    frames,
    renderScale,
    renderer: heatmap.rendererName,
    symbol: currentSymbol,
  };
  const mode = continuous ? "" : " (bei Änderung)";
  const sw = heatmap.isSoftware ? " · Software-GL" : "";
  perfEl.textContent =
    `${fps.toFixed(0)} FPS${mode} · Frame ${w.avg.toFixed(1)} ms (p95 ${w.p95.toFixed(1)})` +
    ` · Skala ${Math.round(renderScale * 100)} %${sw}`;
}, 1000);

if (params.has("bench")) {
  // Komplettes Rendern inkl. GPU-Abschluss (readPixels erzwingt Synchronisation)
  window.__cockbookBench = (n: number) => {
    const px = new Uint8Array(4);
    const gl = heatmap.gl;
    const t = performance.now();
    for (let i = 0; i < n; i++) {
      render();
      gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
    }
    return { renderMs: (performance.now() - t) / n, pixels: glCanvas.width * glCanvas.height };
  };
}
