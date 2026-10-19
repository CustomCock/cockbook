import "./style.css";
import { AXIS_H, AXIS_W, drawPriceAxis, drawTimeAxis, formatPrice, gridStepTicks, timeLabel, type Instrument } from "./axes";
import { aggregate, buildCandles, CandleSeries, RRectBuilder, type CandleMinutes } from "./candles";
import { computeRange, type ScaleRange, type ScaleSettings } from "./colorscale";
import { showTokenPrompt, takeTokenFromHash, wsUrlWithToken } from "./backend";
import { CoinPicker } from "./coinpicker";
import { ColumnAssembler } from "./columns";
import { CYCLE_PRESETS } from "./cycles";
import { buildDomRows, DOM_W, drawDom } from "./dom";
import { loadFeatures, saveFeatures } from "./features";
import { HeatmapRenderer, PRIM, PrimRenderer, RRectRenderer } from "./gl";
import { coarseView, type LongParams } from "./longterm";
import { LongView, type LongLayers } from "./longview";
import { Markets, decimalsOf } from "./markets";
import { connect, toFloat32, toInt32, type Hello } from "./net";
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
import { drawRsiPane, RSI_PANE_H } from "./rsipane";
import { Sidebar, type CycleChoice } from "./sidebar";
import { baseSymbol, isLongTab, LONG_SUFFIX, TabBar, TabModel } from "./tabs";
import { downsample, HeatmapStore, rowsFor, type TradeBin } from "./store";
import { coinOf, keyFor, venueOf } from "./venue";
import { clickOrder, type TradeLine } from "./paper";
import { TradeOverlay } from "./tradeoverlay";
import { TradePanel, type MarketCtx } from "./tradepanel";
import { panY, priceUnit, recenter, unitToY, visibleCols, xToCol, yToUnit, zoomX, zoomY, type View } from "./viewport";
import { DrawingController } from "./drawings/controller";
import type { Ctx as DrawCtx } from "./drawings/geom";
import type { Drawing } from "./drawings/model";
import { DrawToolbar } from "./drawings/toolbar";
import { AiPanel } from "./aipanel";

takeTokenFromHash();
const SETTINGS_KEY = "cockbook.settings.v1";
const SYMBOL_KEY = "cockbook.symbol.v1";
const CANDLE_KEY = "cockbook.candles.v1";
const CYCLE_KEY = "cockbook.cycle.v1";
const MAX_HISTORY_COLS = 4096;
const DOM_KEY = "cockbook.dom.v1";
/** So viele Coins bleiben samt Heatmap im Speicher (schneller Tab-Wechsel). */
const CACHE_SYMBOLS = 4;
/** Langzeit-Ebene: höchstens so viele Zeilen (Speicher = Spalten x Zeilen x 4 Byte) */
/** Zweite Preis-Ebene zum Herauszoomen: ein Bucket = so viele feine */
const MIP_FACTOR = 8;
const LONG_TIER_KEY = "cockbook.longtier.v1";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const chartEl = $<HTMLElement>("chart");
const glCanvas = $<HTMLCanvasElement>("gl");
const tradeCanvas = $<HTMLCanvasElement>("tradeov");
const drawCanvas = $<HTMLCanvasElement>("drawov");
const chartRow = $<HTMLElement>("chartrow");
const drawStyleEl = $<HTMLElement>("drawstyle");
const autoBtn = $<HTMLButtonElement>("autoScale");
const tradeOverlay = new TradeOverlay(tradeCanvas);
/** zählt Änderungen am Paper-Zustand bzw. an gezogenen Linien (neu zeichnen) */
let tradeVersion = 0;
const yAxisCanvas = $<HTMLCanvasElement>("yaxis");
const xAxisCanvas = $<HTMLCanvasElement>("xaxis");
const rsiCanvas = $<HTMLCanvasElement>("rsi");
const domCanvas = $<HTMLCanvasElement>("dom");
const ltInfo = $<HTMLElement>("ltinfo");
const crossV = $<HTMLElement>("crossV");
const crossH = $<HTMLElement>("crossH");
const tooltipEl = $<HTMLElement>("tooltip");
const contrastEl = $<HTMLInputElement>("contrast");
const thresholdEl = $<HTMLInputElement>("threshold");
const perfEl = $<HTMLElement>("perf");

const heatmap = new HeatmapRenderer(glCanvas);
// Grobe Preis-Ebene (Herauszoomen): zweiter Renderer auf demselben Canvas/Kontext
const heatmapMip = new HeatmapRenderer(glCanvas);
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
/** Grobe Preis-Ebene: dieselben Spalten, je MIP_FACTOR Buckets zusammengefasst (Maximum) */
let mip: HeatmapStore | null = null;
// Bucket-Zustand aus Schlüsselbildern + Änderungs-Spalten (gehört zu store)
let assembler = new ColumnAssembler();
let view: View = { yLo: 0, yHi: 100, colWidth: 2, offset: 0, autoCenter: true };
let viewInitialized = false;
let range: ScaleRange | null = null;
/** Langzeit-Tab aktiv (Live-Chart ausgeblendet) */
let longActive = false;
let longTier = loadJson(LONG_TIER_KEY, { tier: "1D" }).tier;

/** Zustand eines Coins, der beim Tab-Wechsel im Speicher bleibt. */
interface SymbolState {
  hello: Hello | null;
  inst: Instrument | null;
  store: HeatmapStore | null;
  mip: HeatmapStore | null;
  assembler: ColumnAssembler;
  candles: CandleSeries;
  view: View;
  viewInitialized: boolean;
}
const cache = new Map<string, SymbolState>(); // Einfügereihenfolge = zuletzt benutzt
let dpr = window.devicePixelRatio || 1;
let mouse: { x: number; y: number } | null = null;
let columnsReceived = 0;
let dataVersion = 0;
let sizeVersion = 0;
let featureVersion = 0;

const markets = new Markets();
const features = loadFeatures();
let candles = new CandleSeries();
let domUsd = loadJson(DOM_KEY, { usd: false }).usd;

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

/** Abbildung feine -> grobe Preis-Ebene (gleiche Zeitachse, y / MIP_FACTOR). */
function mipParams(): LongParams | null {
  return hello ? { fineIntervalMs: hello.interval_ms, longIntervalMs: hello.interval_ms, factor: MIP_FACTOR } : null;
}

/** Zeit der neuesten Spalte = x-Bezug aller Ebenen. */
function latestTs(): number | null {
  return store?.latestTs ?? null;
}

/** Sicht der groben Preis-Ebene (null = keine Daten). */
function mipView(): View | null {
  const mp = mipParams();
  if (!mp || !mip || mip.count === 0) return null;
  const t = mip.ts[mip.latest]!;
  return coarseView(view, t, t, mp);
}

function fmtSpan(ms: number): string {
  const min = Math.round(ms / 60_000);
  return min < 60 ? `${min} min` : `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, "0")} min`;
}

/** Anzeige im Kopf: wie weit die Live-Heatmap zurückreicht. */
function updateLongInfo(): void {
  const fo = store?.oldestTs;
  ltInfo.textContent = fo != null && !longActive ? `Live-Historie ${fmtSpan(Date.now() - fo)}` : "";
  ltInfo.title =
    "Volle Auflösung (100 ms), nur im Speicher. Für Stunden bis Wochen: Langzeit-Heatmap (Seitenleiste).";
}

// --- Layout ------------------------------------------------------------------------------

function rsiHeight(): number {
  return features.rsi ? RSI_PANE_H : 0;
}

function domWidth(): number {
  return features.dom ? DOM_W : 0;
}

function plotSize(): { pw: number; ph: number } {
  return {
    pw: Math.max(1, chartEl.clientWidth - AXIS_W - domWidth()),
    ph: Math.max(1, chartEl.clientHeight - AXIS_H - rsiHeight()),
  };
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
  place(tradeCanvas, 0, 0, pw, ph, dpr);
  place(drawCanvas, 0, 0, pw, ph, dpr);
  const rh = rsiHeight();
  place(yAxisCanvas, pw, 0, AXIS_W, ph, dpr);
  domCanvas.style.display = features.dom ? "block" : "none";
  if (features.dom) place(domCanvas, pw + AXIS_W, 0, DOM_W, ph + rsiHeight() + AXIS_H, dpr);
  rsiCanvas.style.display = rh ? "block" : "none";
  if (rh) place(rsiCanvas, 0, ph, pw + AXIS_W, rh, dpr);
  place(xAxisCanvas, 0, ph + rh, pw, AXIS_H, dpr);
  // Ecke unter der Preisleiste (wie in TradingView): Auto-Knopf
  Object.assign(autoBtn.style, { left: `${pw}px`, top: `${ph + rh}px`, width: `${AXIS_W}px`, height: `${AXIS_H}px` });
  crossV.style.height = `${ph + rh}px`;
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
    candles1m: () => candles.all(),
    onOpenLong: () => {
      if (currentSymbol) activateTab(currentSymbol + LONG_SUFFIX);
    },
    onOpenLive: () => {
      if (longView.symbol) activateTab(longView.symbol);
    },
    onLongTier: (t) => longView.setTier(t),
    onToggle: (id, on) => {
      saveFeatures(features);
      featureVersion++;
      if (id.startsWith("long")) longView.setLayers(longLayers());
      if (id === "candles" && on) fitToCandles();
      if (id === "rsi" || id === "dom") resize();
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
// Coin aus dem Menü: im Langzeit-Tab dessen Langzeit-Ansicht, sonst Live-Chart
const picker = new CoinPicker($<HTMLElement>("picker"), markets, (sym) =>
  activateTab(longActive ? sym + LONG_SUFFIX : sym),
);
const tabModel = new TabModel();
tabModel.load();
const tabs = new TabBar($<HTMLElement>("tabs"), markets, tabModel, {
  onActivate: (id) => activateTab(id),
  onAdd: () => picker.open(),
});
tabs.render();
if (tabModel.active) currentSymbol = baseSymbol(tabModel.active);
setInterval(() => {
  const now = Date.now();
  tradePanel.setMarket(marketCtx());
  domCanvas.style.cursor = tradePanel.clickTrading ? "pointer" : "";
  sidebar.update(now);
  picker.update(now);
  tabs.update();
  updateLongInfo();
}, 500);

domCanvas.addEventListener("click", (e) => {
  // Klick auf die Kopfzeile: Menge <-> USDT
  if (e.offsetY < 20) {
    domUsd = !domUsd;
    saveJson(DOM_KEY, { usd: domUsd });
    featureVersion++;
    return;
  }
  // Klick-Trading: BID-Spalte = kaufen, ASK-Spalte = verkaufen (Preis = Mausposition)
  const ctx = marketCtx();
  const { ph } = plotSize();
  if (!tradePanel.clickTrading || !ctx || e.offsetY >= ph) return;
  const where = e.offsetX < 74 ? "bid" : e.offsetX < 148 ? "ask" : null;
  if (!where) return;
  const price = priceAtY(e.offsetY);
  if (price === null) return;
  const { side, kind } = clickOrder(where, price, ctx.bid, ctx.ask);
  tradePanel.placeAt(side, kind, price);
});

/** Markt für das Handels-Panel: Symbol, Raster und aktuelles BBO. */
function marketCtx(): MarketCtx | null {
  if (!currentSymbol || !inst || !store || store.count === 0) return null;
  let bid = NaN;
  let ask = NaN;
  for (let k = 0; k < Math.min(5, store.count) && (Number.isNaN(bid) || Number.isNaN(ask)); k++) {
    const i = store.indexAgo(k);
    bid = store.bestBid[i]!;
    ask = store.bestAsk[i]!;
  }
  if (Number.isNaN(bid) || Number.isNaN(ask)) return null;
  const step = markets.bySymbol.get(currentSymbol)?.stepSize ?? inst.tickSize;
  return { symbol: currentSymbol, tick: inst.tickSize, step, bid: bid * inst.tickSize, ask: ask * inst.tickSize };
}

/** Preis an einer y-Position im Chart (null ohne Instrument). */
function priceAtY(y: number): number | null {
  if (!inst) return null;
  const { ph } = plotSize();
  return yToUnit(y, view, ph) * inst.bucketTicks * inst.tickSize;
}


function setBadge(id: string, text: string, cls: string): void {
  const el = $<HTMLElement>(id);
  el.textContent = text;
  el.className = `badge ${cls}`;
}

function rememberCurrent(): void {
  if (!currentSymbol || !store) return;
  cache.delete(currentSymbol);
  cache.set(currentSymbol, { hello, inst, store, mip, assembler, candles, view, viewInitialized });
  while (cache.size > CACHE_SYMBOLS) cache.delete(cache.keys().next().value!);
}

function longLayers(): LongLayers {
  return { heat: !!features.longHeat, candles: !!features.longCandles, liqs: !!features.longLiqs, dom: !!features.longDom };
}

/** Tab aktivieren: Live-Chart ("BTCUSDT") oder Langzeit-Heatmap ("BTCUSDT@long"). */
function activateTab(id: string): void {
  if (isLongTab(id)) openLong(baseSymbol(id));
  else selectSymbol(id);
}

function openLong(sym: string): void {
  // Das kombinierte Buch sammelt keine eigene Langzeit-Historie -> die von Binance zeigen
  if (venueOf(sym) === "multi") sym = keyFor("binance", coinOf(sym));
  tabModel.open(sym + LONG_SUFFIX);
  tabs.render();
  longActive = true;
  chartEl.hidden = true;
  chartRow.hidden = true;
  longView.setLayers(longLayers());
  longView.open(sym, longTier);
  sidebar.setMode("long", longTier);
  sidebar.setActive(sym);
  picker.setActive(sym);
  setBadge("sync", "Langzeit", "");
  updateLongInfo();
}

function leaveLong(): void {
  if (!longActive) return;
  longActive = false;
  longView.stop();
  chartEl.hidden = false;
  chartRow.hidden = false;
  sidebar.setMode("live");
  resize();
  dataVersion++;
}

function selectSymbol(sym: string): void {
  tabModel.open(sym);
  tabs.render();
  const wasLong = longActive;
  leaveLong();
  drawCtl.setSymbol(sym);
  if (sym === currentSymbol && store) {
    if (wasLong) {
      sidebar.setActive(sym);
      picker.setActive(sym);
    }
    return;
  }
  rememberCurrent();
  currentSymbol = sym;
  try {
    localStorage.setItem(SYMBOL_KEY, sym);
  } catch {
    /* egal */
  }
  const cached = cache.get(sym);
  range = null;
  if (cached?.store) {
    // Tab-Wechsel zurück: Heatmap liegt noch im Speicher, nur der Rest kommt nach
    ({ hello, inst, mip, assembler, candles, viewInitialized } = cached);
    store = cached.store;
    view = { ...cached.view, colWidth: view.colWidth };
    heatmap.attach(cached.store);
    if (mip) heatmapMip.attach(mip);
    updateRange();
  } else {
    // Chart leeren, bis hello + Historie des neuen Symbols da sind
    hello = null;
    inst = null;
    store = null;
    mip = null;
    assembler = new ColumnAssembler();
    candles = new CandleSeries();
    viewInitialized = false;
    view = { ...view, offset: 0, autoCenter: true };
  }
  dataVersion++;
  sidebar.setActive(sym);
  picker.setActive(sym);
  setBadge("sync", "lade …", "");
  conn.subscribe(sym, store?.latestTs);
}

// --- Daten -------------------------------------------------------------------------------

const conn = connect(
  wsUrlWithToken,
  {
    onLive(m) {
      tradePanel.onLive(m);
      tradeVersion++;
    },
    onLiveResult(m) {
      tradePanel.onLiveResult(m);
      tradeVersion++;
    },
    onPaper(m) {
      tradePanel.onMessage(m);
      tradeVersion++;
    },
    onConnection(ok) {
      if (!ok) setBadge("sync", "keine Verbindung", "off");
      else {
        if (drawCtl.key) conn.draw("load", drawCtl.key);
        aiPanel.refreshStatus();
      }
    },
    onDrawings(m) {
      drawCtl.onServer(m.key, m.items as Drawing[]);
    },
    onAiStatus(p) {
      aiPanel.onStatus(p);
    },
    onAiEvent(chat, ev) {
      aiPanel.onEvent(chat, ev);
    },
    onSymbols(s) {
      for (const info of s.symbols) markets.setInfo(info);
      const list = s.symbols.map((x) => x.symbol);
      picker.setSymbols(list);
      if (!tabModel.tabs.length) {
        // Erster Start: die ersten Coins der Config als Tabs
        tabModel.tabs = list.slice(0, 4);
        tabModel.active = list[0] ?? null;
        tabs.render();
      } else tabs.render();
      if (tabModel.active && isLongTab(tabModel.active) && !longActive && list.includes(baseSymbol(tabModel.active))) {
        activateTab(tabModel.active);
      } else if (longActive) {
        /* Langzeit-Tab offen: nichts umschalten */
      } else if (!currentSymbol || !list.includes(currentSymbol)) {
        const next = tabModel.active && list.includes(baseSymbol(tabModel.active)) ? tabModel.active : list[0];
        if (next) activateTab(next);
      } else {
        sidebar.setActive(currentSymbol);
        picker.setActive(currentSymbol);
      }
    },
    onHello(h) {
      if (currentSymbol && h.symbol !== currentSymbol) return; // verspätet, altes Symbol
      currentSymbol = h.symbol;
      const key = (x: Hello | null) =>
        x &&
        [
          x.exchange, x.symbol, x.tick_size, x.bucket_ticks, x.interval_ms, x.half_range_buckets,
        ].join("|");
      const changed = key(h) !== key(hello);
      hello = h;
      inst = {
        symbol: h.symbol,
        tickSize: Number(h.tick_size),
        tickDecimals: decimalsOf(h.tick_size),
        bucketTicks: h.bucket_ticks,
      };
      markets.ensure(h.symbol, h.tick_size);
      setBadge("mode", h.mode.toUpperCase(), h.mode);
      sidebar.setActive(h.symbol);
      picker.setActive(h.symbol);
      if (changed || !store) {
        const maxTex = heatmap.maxTextureSize;
        store = new HeatmapStore(
          Math.min(MAX_HISTORY_COLS, maxTex),
          rowsFor(Math.min(h.half_range_buckets, 800), maxTex, 2048),
          h.interval_ms,
        );
        heatmap.attach(store);
        assembler = new ColumnAssembler();
        mip = new HeatmapStore(
          Math.min(MAX_HISTORY_COLS, maxTex),
          rowsFor(h.half_range_buckets / MIP_FACTOR, maxTex, 2048),
          h.interval_ms,
        );
        heatmapMip.attach(mip);
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
    onLong(m) {
      longView.onMessage(m);
    },
    onAuthError() {
      setBadge("sync", "Token nötig", "off");
      showTokenPrompt(() => conn.reconnect());
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
        // Auch schon vorhandene Spalten (Historie nach Reconnect) laufen durch den
        // Assembler: Der Server beginnt beim Schlüsselbild, der Bucket-Zustand entsteht neu.
        const col = assembler.apply(f.s, f.k, f.n, f.b, toFloat32(f.q), f.i ? toInt32(f.i) : null);
        if (store.latestTs !== null && f.ts <= store.latestTs) return; // schon vorhanden
        const center = f.c ?? null;
        // col === null bei Lücke: als "nicht synchron" zeigen statt eine falsche Heatmap
        const synced = col !== null;
        const empty = { base: 0, values: new Float32Array(0), idx: new Int32Array(0) };
        const c = col ?? empty;
        store.pushColumn(f.ts, synced, c.base, c.values, c.idx, center);
        if (mip) {
          const d = synced ? downsample(c.base, c.values, c.idx, MIP_FACTOR) : empty;
          mip.pushColumn(f.ts, synced, d.base, d.values, d.idx, center === null ? null : Math.floor(center / MIP_FACTOR));
        }
        columnsReceived++;
        // Beim Zurückscrollen bleibt der betrachtete Zeitpunkt stehen
        if (view.offset > 0) view = { ...view, offset: Math.min(view.offset + 1, maxOffset()) };
      } else if (f.t === "bbo") {
        store.setBbo(f.ts, f.bb, f.ba);
        mip?.setBbo(f.ts, f.bb, f.ba);
        if (!viewInitialized && f.bb !== null && f.ba !== null) {
          // Spalten sind auf das bekannte Buch zugeschnitten (nicht symmetrisch) -> am Mid ausrichten
          const center = priceUnit((f.bb + f.ba) / 2, hello.bucket_ticks);
          const half = hello.ui.initial_view_buckets ?? 250;
          view = { ...view, yLo: center - half, yHi: center + half };
          viewInitialized = true;
        }
        if (view.autoCenter && view.offset <= 0 && f.bb !== null && f.ba !== null) {
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

const tradePanel = new TradePanel(
  $<HTMLElement>("trade"),
  conn,
  markets,
  () => {
    $<HTMLButtonElement>("tradeBtn").classList.toggle("on", tradePanel.open);
    resize();
  },
  (sym) => activateTab(sym),
);
$<HTMLButtonElement>("tradeBtn").classList.toggle("on", tradePanel.open);
$<HTMLButtonElement>("tradeBtn").addEventListener("click", () => tradePanel.toggle());

// --- KI-Panel --------------------------------------------------------------------------------

/** Was der Nutzer gerade sieht – geht mit jeder Frage an die KI ("dieser Coin", "hier"). */
function aiContext(): Record<string, string | number> {
  const ctx: Record<string, string | number> = {};
  if (longActive && longView.symbol) {
    ctx.symbol = longView.symbol;
    ctx.ansicht = `Langzeit-Heatmap ${longTier}`;
    return ctx;
  }
  if (!currentSymbol) return ctx;
  ctx.symbol = currentSymbol;
  ctx.boerse = venueOf(currentSymbol);
  const m = marketCtx();
  if (m) ctx.mid = Number(((m.bid + m.ask) / 2).toFixed(8));
  const d = drawCtx();
  if (d) {
    const iso = (t: number) => new Date(t).toISOString().slice(0, 19) + "Z";
    ctx.sichtbar_von = iso(d.toT(0));
    ctx.sichtbar_bis = iso(d.toT(d.pw));
    ctx.preis_unten = Number(d.toP(d.ph).toFixed(8));
    ctx.preis_oben = Number(d.toP(0).toFixed(8));
  }
  if (features.candles) ctx.kerzen_minuten = candleMinutes;
  return ctx;
}

const aiPanel = new AiPanel($<HTMLElement>("ai"), conn, aiContext, () => {
  $<HTMLButtonElement>("aiBtn").classList.toggle("on", aiPanel.open);
  resize();
});
$<HTMLButtonElement>("aiBtn").classList.toggle("on", aiPanel.open);
$<HTMLButtonElement>("aiBtn").addEventListener("click", () => aiPanel.toggle());
aiPanel.refreshStatus();

// --- Zeichenwerkzeuge ------------------------------------------------------------------------

let drawToolbar: DrawToolbar | null = null;
const drawCtl = new DrawingController(
  {
    put: (key, d) => conn.draw("put", key, { item: d }),
    remove: (key, id) => conn.draw("delete", key, { id }),
    clear: (key) => conn.draw("clear", key),
    load: (key) => conn.draw("load", key),
  },
  (t0, t1, p0, p1) => zoomToRect(t0, t1, p0, p1),
  () => drawToolbar?.refresh(),
);
drawToolbar = new DrawToolbar($<HTMLElement>("drawbar"), drawStyleEl, drawCtl, () => tradeVersion++);

/**
 * Umrechnung Zeit/Preis <-> Pixel für die Zeichnungen. Dieselbe Zeitachse wie Kerzen und
 * Heatmap (rechte Kante = neueste Spalte minus offset), fraktional statt auf Spalten gerundet.
 */
function drawCtx(): DrawCtx | null {
  if (!store || !inst || !hello || store.count === 0) return null;
  const { pw, ph } = plotSize();
  const latest = store.ts[store.latest]!;
  const iv = hello.interval_ms;
  const i = inst;
  const s = store;
  const v = view;
  const unit = i.tickSize * i.bucketTicks;
  return {
    toX: (t) => pw - ((latest - t) / iv - v.offset) * v.colWidth,
    toT: (x) => latest - ((pw - x) / v.colWidth + v.offset) * iv,
    toY: (p) => unitToY(p / unit, v, ph),
    toP: (y) => yToUnit(y, v, ph) * unit,
    pw,
    ph,
    interval: iv,
    bars: candles.all(),
    trades: (t0, t1) => {
      const out: { p: number; q: number }[] = [];
      for (let k = 0; k < s.count; k++) {
        const idx = s.indexAgo(k);
        const ts = s.ts[idx]!;
        if (ts < t0) break;
        if (ts > t1) continue;
        for (const b of s.trades[idx] ?? []) out.push({ p: b.vwapTicks * i.tickSize, q: b.qty });
      }
      return out;
    },
    fmtP: (p) => formatPrice(Math.round(p / i.tickSize), i),
    fmtT: (t) => timeLabel(t, true),
    tick: i.tickSize,
  };
}

/** Zoom-Werkzeug: aufgezogenes Rechteck (Zeit/Preis) wird die neue Sicht. */
function zoomToRect(t0: number, t1: number, p0: number, p1: number): void {
  if (!store || !inst || !hello || store.count === 0 || t1 <= t0 || p1 <= p0) return;
  const { pw } = plotSize();
  const latest = store.ts[store.latest]!;
  const unit = inst.tickSize * inst.bucketTicks;
  const colWidth = pw / ((t1 - t0) / hello.interval_ms);
  view = zoomX({ ...view, colWidth: 1 }, colWidth);
  const offset = Math.min(Math.max(Math.round((latest - t1) / hello.interval_ms), minOffset()), maxOffset());
  view = { ...view, offset, autoCenter: false, yLo: p0 / unit, yHi: p1 / unit };
}

const longView = new LongView($<HTMLElement>("longchart"), markets, conn, scaleSettings, (t) => {
  longTier = t;
  saveJson(LONG_TIER_KEY, { tier: t });
  sidebar.setMode("long", t);
}, () => domUsd);
longView.hide();

// Token-Link geöffnet, während die Seite schon offen ist (nur der #-Teil ändert sich)
window.addEventListener("hashchange", () => {
  if (takeTokenFromHash()) conn.reconnect();
});

// --- Farbskala (alle 500 ms aus dem sichtbaren Bereich) -----------------------------------

/** Stichprobe der sichtbaren Werte einer Ebene (max. ~200 x 200). */
function sampleVisible(s: HeatmapStore, v: View, pw: number, out: number[]): void {
  const k0 = Math.max(0, Math.floor(v.offset));
  const n = Math.min(visibleCols(v, pw), s.count - k0);
  if (n <= 0) return;
  const colStride = Math.max(1, Math.floor(n / 200));
  const r0 = Math.max(0, Math.floor(v.yLo) - s.origin);
  const r1 = Math.min(s.rows, Math.ceil(v.yHi) - s.origin);
  const rowStride = Math.max(1, Math.floor((r1 - r0) / 200));
  for (let k = k0; k < k0 + n; k += colStride) {
    const base = s.indexAgo(k) * s.rows;
    for (let r = r0; r < r1; r += rowStride) {
      const x = s.data[base + r]!;
      if (x > 0) out.push(x);
    }
  }
}

function updateRange(): void {
  const { pw } = plotSize();
  const out: number[] = [];
  if (store && store.count) sampleVisible(store, view, pw, out);
  // Herausgezoomt: Farbskala auch aus der groben Ebene (dort liegt dann der Großteil)
  const mv = mipView();
  if (mv && mip) sampleVisible(mip, mv, pw, out);
  if (!out.length) return;
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
    const { pw, ph } = plotSize();
    const { x, y } = localPos(e);
    if (x >= pw && x < pw + AXIS_W && y < ph) {
      // über der Preisleiste: Preis-Zoom um die Mausposition
      view = { ...zoomY(view, factor, yToUnit(y, view, ph)), autoCenter: false };
    } else {
      // im Chart und auf der Zeitleiste: Zeit-Zoom (wie TradingView)
      view = clampOffset(zoomX(view, 1 / factor));
      saveSettings();
      if (features.candles && view.autoCenter) fitToCandles();
    }
    updateHover();
  },
  { passive: false },
);

let drag: { x: number; y: number; view: View } | null = null;
/** gezogene Handels-Linie (Order/TP/SL) */
let lineDrag: TradeLine | null = null;
/** Maus gehört gerade den Zeichenwerkzeugen (Punkt setzen, Zeichnung ziehen, messen) */
let drawDrag = false;
/** Preis- oder Zeitleiste wird gezogen (skalieren) */
let axisDrag: { axis: "price" | "time"; x: number; y: number; view: View } | null = null;
/** Pixel Mausweg für Faktor e (Achse ziehen): größer = feinfühliger */
const AXIS_DRAG_PX = 160;
chartEl.addEventListener("pointerdown", (e) => {
  // DOM-Spalte hat eigene Klicks (Kopf: Einheit, Klick-Trading) – nicht einfangen, sonst
  // käme der click nie am DOM-Canvas an
  if (e.target === domCanvas || e.target === autoBtn || drawStyleEl.contains(e.target as Node)) return;
  const p = localPos(e);
  const { pw, ph } = plotSize();
  if (e.button === 0 && (e.target === yAxisCanvas || e.target === xAxisCanvas)) {
    // Achse ziehen = skalieren (Preisleiste: Preis, Zeitleiste: Zeit) wie in TradingView
    axisDrag = { axis: e.target === yAxisCanvas ? "price" : "time", x: e.clientX, y: e.clientY, view };
    chartEl.setPointerCapture(e.pointerId);
    return;
  }
  const dctx = drawCtx();
  if (e.button === 0 && p.x < pw && p.y < ph) {
    // Aktives Zeichenwerkzeug hat Vorrang vor Handelslinien und Verschieben
    if (dctx && drawCtl.busy && drawCtl.pointerDown(dctx, p.x, p.y, e)) {
      drawDrag = true;
      chartEl.setPointerCapture(e.pointerId);
      return;
    }
    const hit = drawCtl.hideTrading ? null : tradeOverlay.hit(p.x, p.y);
    if (hit?.cancel && hit.line.cancel) {
      tradePanel.cancelLine(hit.line.cancel);
      return;
    }
    if (hit?.line.drag) {
      lineDrag = hit.line;
      tradeOverlay.dragging = { line: hit.line, price: hit.line.price };
      chartEl.setPointerCapture(e.pointerId);
      return;
    }
    const ctx = marketCtx();
    if ((e.ctrlKey || e.metaKey) && tradePanel.clickTrading && ctx) {
      const price = priceAtY(p.y);
      if (price !== null) {
        const { side, kind } = clickOrder("chart", price, ctx.bid, ctx.ask);
        tradePanel.placeAt(side, kind, price);
      }
      return;
    }
    if (dctx && drawCtl.pointerDown(dctx, p.x, p.y, e)) {
      drawDrag = true;
      chartEl.setPointerCapture(e.pointerId);
      return;
    }
  }
  drag = { x: e.clientX, y: e.clientY, view };
  chartEl.setPointerCapture(e.pointerId);
});
chartEl.addEventListener("pointermove", (e) => {
  mouse = localPos(e);
  if (axisDrag) {
    const a = axisDrag;
    if (a.axis === "price") {
      // nach unten ziehen = mehr Preisbereich (gestaucht), nach oben = gedehnt; Mitte bleibt
      const f = Math.exp((e.clientY - a.y) / AXIS_DRAG_PX);
      view = { ...zoomY(a.view, f, (a.view.yLo + a.view.yHi) / 2), autoCenter: false };
    } else {
      // nach links ziehen = enger (mehr Zeit), nach rechts = weiter
      view = clampOffset(zoomX({ ...a.view, offset: view.offset }, Math.exp((e.clientX - a.x) / AXIS_DRAG_PX)));
    }
    updateHover();
    return;
  }
  if (lineDrag && tradeOverlay.dragging) {
    const price = priceAtY(mouse.y);
    if (price !== null) tradeOverlay.dragging = { line: lineDrag, price };
    tradeVersion++;
    updateHover();
    return;
  }
  const dctx = drawCtx();
  if (dctx && drawCtl.pointerMove(dctx, mouse.x, mouse.y, e)) {
    updateHover();
    return;
  }
  if (!drag) {
    const inPlot = mouse.x < plotSize().pw;
    if (e.target === yAxisCanvas || e.target === xAxisCanvas) {
      chartEl.style.cursor = e.target === yAxisCanvas ? "ns-resize" : "ew-resize";
      updateHover();
      return;
    }
    const hit = inPlot && !drawCtl.hideTrading ? tradeOverlay.hit(mouse.x, mouse.y) : null;
    const over = inPlot && !!dctx && drawCtl.hoverHit(dctx, mouse.x, mouse.y);
    chartEl.style.cursor = hit
      ? hit.cancel
        ? "pointer"
        : hit.line.drag
          ? "ns-resize"
          : ""
      : drawCtl.cssCursor(over);
  }
  if (drag && store) {
    const { ph } = plotSize();
    const dy = e.clientY - drag.y;
    const dx = e.clientX - drag.x;
    const unitsPerPx = (drag.view.yHi - drag.view.yLo) / ph;
    let v = Math.abs(dy) > 2 ? panY(drag.view, dy * unitsPerPx) : { ...drag.view };
    const offset = Math.round(drag.view.offset + dx / drag.view.colWidth);
    v = clampOffset({ ...v, offset });
    if (v.offset > 0) v.autoCenter = false;
    view = v;
  }
  updateHover();
});
chartEl.addEventListener("pointerup", (e) => {
  drag = null;
  if (axisDrag) {
    axisDrag = null;
    saveSettings();
  }
  if (drawDrag) {
    drawDrag = false;
    const dctx = drawCtx();
    const p = localPos(e);
    if (dctx) drawCtl.pointerUp(dctx, p.x, p.y);
  }
  if (lineDrag && tradeOverlay.dragging && lineDrag.drag) {
    const moved = Math.abs(tradeOverlay.dragging.price - lineDrag.price) > (inst?.tickSize ?? 0) / 2;
    if (moved) tradePanel.dragTo(lineDrag.drag, tradeOverlay.dragging.price);
  }
  lineDrag = null;
  tradeOverlay.dragging = null;
  tradeVersion++;
});
chartEl.addEventListener("pointerleave", () => {
  mouse = null;
  updateHover();
});
chartEl.addEventListener("dblclick", (e) => {
  if (drawStyleEl.contains(e.target as Node) || e.target === autoBtn) return;
  if (e.target === yAxisCanvas) {
    autoScale(true);
    return;
  }
  if (drawCtl.doubleClick()) return;
  // Doppelklick auf eine Zeichnung soll nicht zur Gegenwart springen
  const dctx = drawCtx();
  const p = localPos(e);
  if (dctx && drawCtl.hoverHit(dctx, p.x, p.y)) return;
  goLive();
});
$<HTMLButtonElement>("live").addEventListener("click", goLive);
window.addEventListener("keydown", (e) => {
  if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return;
  if (longActive) return;
  if (e.target instanceof HTMLTextAreaElement || (e.target instanceof HTMLElement && e.target.closest("#trade"))) return;
  if (drawCtl.key_(e)) {
    e.preventDefault();
    return;
  }
  if (e.ctrlKey || e.metaKey) return;
  if (e.key === "c" || e.key === "C") goLive();
  if (e.key === "f" || e.key === "F") fitToCandles();
  if ((e.key === "t" || e.key === "T") && !e.ctrlKey && !e.metaKey && !e.altKey) tradePanel.toggle();
  if ((e.key === "k" || e.key === "K") && !e.altKey) aiPanel.toggle();
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

/**
 * Wie weit man nach rechts über die neueste Spalte hinaus scrollen kann (negativer offset):
 * bis 85 % der Breite leer. Neue Spalten bauen sich dann links davon weiter auf, der Abstand
 * zum rechten Rand bleibt – wie der Rand in TradingView.
 */
function minOffset(): number {
  return -Math.floor(visibleCols(view, plotSize().pw) * 0.85);
}

function clampOffset(v: View): View {
  const offset = Math.min(Math.max(v.offset, minOffset()), maxOffset());
  return { ...v, offset, autoCenter: offset > 0 ? false : v.autoCenter };
}

/**
 * Auto (Knopf unter der Preisleiste, Doppelklick auf die Preisleiste): Preisachse folgt dem
 * Kurs – zentriert auf den Mid bzw. bei Kerzen an die sichtbaren Kerzen angepasst. Die
 * Zeitachse bleibt, wo sie ist.
 */
function autoScale(on: boolean): void {
  view = { ...view, autoCenter: on };
  if (!on || !store || !hello || store.count === 0) return;
  if (features.candles) {
    fitToCandles();
    return;
  }
  const i = store.latest;
  const bb = store.bestBid[i]!;
  const ba = store.bestAsk[i]!;
  if (!Number.isNaN(bb) && !Number.isNaN(ba)) view = recenter(view, priceUnit((bb + ba) / 2, hello.bucket_ticks), 0, true);
}
autoBtn.addEventListener("click", () => autoScale(!view.autoCenter));

/** Wie weit man zurückscrollen kann: Heatmap-Historie oder – mit Kerzen – deren Historie. */
function maxOffset(): number {
  if (!store || store.count === 0 || !hello) return 0;
  const latest = store.ts[store.latest]!;
  let cols = store.count - 1;
  const first = candles.first;
  if (features.candles && first !== null) {
    cols = Math.max(cols, Math.floor((latest - first) / hello.interval_ms));
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
  const latest = latestTs();
  if (!store || !inst || !hello || latest === null) {
    tooltipEl.style.display = "none";
    return;
  }
  const k = xToCol(mouse.x, view, pw);
  const unit = yToUnit(mouse.y, view, ph);
  const fmtQty = (q: number) => q.toLocaleString("de-DE", { maximumFractionDigits: 3 });
  const priceRange = (lo: number, n: number) =>
    n === 1 ? formatPrice(lo, inst!) : `${formatPrice(lo, inst!)} – ${formatPrice(lo + n - 1, inst!)}`;
  const lines: string[] = [];
  if (k < 0) {
    // rechts der neuesten Spalte (Zukunft): nur Zeit
    tooltipEl.textContent = timeLabel(latest - k * hello.interval_ms, true);
  }
  if (k < 0 || k >= store.count) {
    if (k >= store.count) tooltipEl.style.display = "none";
    else placeTooltip(pw, ph);
    return;
  }
  const bucket = Math.floor(unit);
  const r = bucket - store.origin;
  if (r >= 0 && r < store.rows) {
    lines.push(priceRange(bucket * inst.bucketTicks, inst.bucketTicks), `Liquidität: ${fmtQty(store.valueAt(k, bucket))}`);
  } else if (mip) {
    // Außerhalb der feinen Ebene: grobe Ebene (größte Stufe im Bereich)
    const cb = Math.floor(unit / MIP_FACTOR);
    const n = inst.bucketTicks * MIP_FACTOR;
    lines.push(priceRange(cb * n, n), `Liquidität (max. Stufe): ${fmtQty(mip.valueAt(k, cb))}`);
  }
  lines.push(timeLabel(latest - k * hello.interval_ms, true));
  tooltipEl.textContent = lines.join("\n");
  placeTooltip(pw, ph);
}

function placeTooltip(pw: number, ph: number): void {
  if (!mouse) return;
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
  heatmapMip.upload();
  const mv = features.heatmap ? mipView() : null;
  // Feine Buckets je Bildschirm-Pixel: ab ~3 bringt die feine Ebene nichts mehr
  const bucketsPerPx = (view.yHi - view.yLo) / (ph * px);
  if (mv && range) {
    // Grobe Ebene überall, die feine darüber, soweit sie Daten hat
    heatmapMip.draw(mv, range, px, features.smooth);
    if (bucketsPerPx < 3) heatmap.draw(view, range, px, features.smooth, true);
  } else {
    heatmap.draw(view, features.heatmap ? range : null, px, features.smooth);
  }
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
  const dctx = drawCtx();
  if (dctx) drawCtl.draw(drawCanvas, dpr, dctx);
  else drawCanvas.getContext("2d")!.clearRect(0, 0, drawCanvas.width, drawCanvas.height);
  autoBtn.classList.toggle("on", view.autoCenter);
  drawPriceAxis(yAxisCanvas, dpr, store, view, inst, ph);
  if (inst && currentSymbol && tradePanel.open && !drawCtl.hideTrading) {
    const i = inst;
    tradeOverlay.draw(
      dpr,
      pw,
      ph,
      tradePanel.lines(currentSymbol),
      (price) => unitToY(price / i.tickSize / i.bucketTicks, view, ph),
      (price) => formatPrice(Math.round(price / i.tickSize), i),
    );
  } else tradeOverlay.draw(dpr, pw, ph, [], () => 0, String);
  if (features.dom) {
    const data = store && inst ? buildDomRows(store, view, pw, ph, inst.bucketTicks) : null;
    drawDom(domCanvas, dpr, data, view, ph, inst?.bucketTicks ?? 1, { usd: domUsd, tickSize: inst?.tickSize ?? 1 });
  }
  drawTimeAxis(xAxisCanvas, dpr, latestTs(), view, hello?.interval_ms ?? 100, pw);
  if (features.rsi) {
    const scene =
      store && inst && hello && store.count
        ? { store, view, pw, ph, intervalMs: hello.interval_ms, tickSize: inst.tickSize, bucketTicks: inst.bucketTicks }
        : null;
    drawRsiPane(rsiCanvas, dpr, aggregate(candles.all(), candleMinutes), candleMinutes, scene, pw);
  }
}

let lastSig: unknown[] = [];
/** Neu zeichnen nur, wenn sich etwas geändert hat (Daten, Sicht, Skala, Größe, Schalter). */
function changed(): boolean {
  const sig = [dataVersion, view, range, sizeVersion, hello, featureVersion, domUsd, tradeVersion, drawCtl.version];
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
  // Effekt-Cursor (Laserpointer, Glitzerspur) verblassen -> solange laufend zeichnen
  if (!continuous && !changed() && !drawCtl.animating) return;
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
