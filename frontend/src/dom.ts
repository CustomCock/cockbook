/**
 * DOM-Ladder (Depth of Market) rechts neben der Preisachse, Zeile für Zeile auf
 * derselben Preisachse wie die Heatmap:
 *
 *   Bid | Ask | Volumen (Kauf/Verkauf) | Δ (Kauf - Verkauf)
 *
 * - Bid/Ask: Liquidität der Spalte am rechten Bildrand (live = jetzt; zurückgescrollt =
 *   Zustand zu diesem Zeitpunkt). Quelle sind die Heatmap-Buckets – dieselben Zahlen,
 *   die die Farbe ergeben, kein zweiter Datenstrom.
 * - Volumen/Δ: gehandelte Menge je Preiszeile im *sichtbaren Zeitraum* (Volume Profile).
 *
 * Zeilenhöhe: Buckets werden zu runden Gruppen (1, 2, 5, 10 … Buckets) zusammengefasst,
 * sodass eine Zeile mindestens `MIN_ROW_PX` hoch ist – lesbar bei jedem Zoom.
 */

import { niceStep } from "./axes";
import type { HeatmapStore } from "./store";
import { unitToY, visibleCols, type View } from "./viewport";

export const DOM_W = 264;
const MIN_ROW_PX = 15;
const HEADER_H = 20;

export interface DomRow {
  bucket: number; // erster Bucket der Zeile
  bid: number;
  ask: number;
  buy: number;
  sell: number;
  isBestBid: boolean;
  isBestAsk: boolean;
}

export interface DomData {
  rowBuckets: number;
  rows: DomRow[];
  /** Spalte, deren Buch angezeigt wird (k ab der neuesten), null = keine Daten */
  k: number | null;
}

/** Buckets je DOM-Zeile: kleinste "runde" Zahl mit Zeilenhöhe >= MIN_ROW_PX. */
export function rowBucketsFor(view: View, ph: number, minRowPx = MIN_ROW_PX): number {
  const pxPerBucket = ph / (view.yHi - view.yLo);
  const raw = minRowPx / pxPerBucket;
  return raw <= 1 ? 1 : Math.round(niceStep(raw));
}

/**
 * `valueScale`: Faktor auf die Bucket-Werte. Die Langzeit-Heatmap speichert Dichte je feinem
 * Bucket (damit alle Stufen dieselbe Farbskala vertragen); fürs Orderbuch zählt die Menge je
 * grobem Bucket = Dichte x Faktor.
 */
export function buildDomRows(
  store: HeatmapStore,
  view: View,
  pw: number,
  ph: number,
  bucketTicks: number,
  valueScale = 1,
): DomData {
  const rb = rowBucketsFor(view, ph);
  const first = Math.floor(view.yLo / rb);
  const last = Math.floor(view.yHi / rb);
  const rows: DomRow[] = [];
  const index = new Map<number, DomRow>();
  for (let r = first; r <= last; r++) {
    const row: DomRow = { bucket: r * rb, bid: 0, ask: 0, buy: 0, sell: 0, isBestBid: false, isBestAsk: false };
    rows.push(row);
    index.set(r, row);
  }
  if (store.count === 0) return { rowBuckets: rb, rows, k: null };
  // Spalte am rechten Rand; deren BBO kommt im eigenen Frame kurz nach der Spalte ->
  // notfalls die vorige Spalte mit vollständigem Buch nehmen
  let k = Math.max(0, Math.min(Math.floor(view.offset), store.count - 1));
  const kEnd = Math.min(k + 5, store.count);
  while (k < kEnd - 1 && !hasBook(store, k)) k++;
  const i = store.indexAgo(k);
  const bb = store.bestBid[i]!;
  const ba = store.bestAsk[i]!;
  const known = hasBook(store, k);
  if (known) {
    const bbRow = Math.floor(Math.floor(bb / bucketTicks) / rb);
    const baRow = Math.floor(Math.floor(ba / bucketTicks) / rb);
    for (const row of rows) {
      for (let b = row.bucket; b < row.bucket + rb; b++) {
        const v = store.valueAt(k, b) * valueScale;
        if (!v) continue;
        // Bucket-Grenze statt Einzelpreis: Bucket mit dem Best Bid zählt zur Bid-Seite
        if (b * bucketTicks <= bb) row.bid += v;
        else row.ask += v;
      }
    }
    const bidRow = index.get(bbRow);
    if (bidRow) bidRow.isBestBid = true;
    const askRow = index.get(baRow);
    if (askRow) askRow.isBestAsk = true;
  }
  // Volumen im sichtbaren Zeitraum (nur feine Spalten tragen Trades)
  const n = Math.min(visibleCols(view, pw), store.count - k);
  for (let j = k; j < k + n; j++) {
    const trades = store.trades[store.indexAgo(j)];
    if (!trades) continue;
    for (const t of trades) {
      const row = index.get(Math.floor(Math.floor(t.vwapTicks / bucketTicks) / rb));
      if (!row) continue;
      if (t.side > 0) row.buy += t.qty;
      else row.sell += t.qty;
    }
  }
  return { rowBuckets: rb, rows, k: known ? k : null };
}

function hasBook(store: HeatmapStore, k: number): boolean {
  const i = store.indexAgo(k);
  return store.synced[i] === 1 && !Number.isNaN(store.bestBid[i]!) && !Number.isNaN(store.bestAsk[i]!);
}

/** Kompakte Mengen: 0,52 · 12,4 · 3,1k · 4,6M */
export function fmtQty(v: number): string {
  const a = Math.abs(v);
  if (a === 0) return "";
  const f = (x: number, d: number) => x.toLocaleString("de-DE", { maximumFractionDigits: d, minimumFractionDigits: 0 });
  if (a >= 1e9) return `${f(v / 1e9, 2)}G`;
  if (a >= 1e6) return `${f(v / 1e6, 2)}M`;
  if (a >= 1e4) return `${f(v / 1e3, 1)}k`;
  if (a >= 100) return f(v, 0);
  if (a >= 1) return f(v, 2);
  return f(v, 3);
}

const C = {
  bg: "#222326",
  head: "#2a2b2f",
  grid: "rgba(255,255,255,0.035)",
  text: "#c9ccd1",
  muted: "#80848b",
  bid: "rgba(85, 229, 168, 0.30)",
  ask: "rgba(255, 115, 133, 0.30)",
  bidText: "#7af0bd",
  askText: "#ff98a5",
  buy: "rgba(85, 229, 168, 0.55)",
  sell: "rgba(255, 115, 133, 0.55)",
  bestBid: "rgba(85, 229, 168, 0.14)",
  bestAsk: "rgba(255, 115, 133, 0.14)",
} as const;

// Spalten (x, Breite)
const COLS = { bid: [0, 74], ask: [74, 74], vol: [148, 72], delta: [220, 44] } as const;

export interface DomOptions {
  /** Werte in USDT statt in Basis-Menge */
  usd: boolean;
  /** Preis je Tick (für USDT-Umrechnung) */
  tickSize: number;
  /** Langzeit: Buch = Mittel über das Intervall, kein Volumen je Preis */
  average?: boolean;
}

export function drawDom(
  canvas: HTMLCanvasElement,
  dpr: number,
  data: DomData | null,
  view: View,
  ph: number,
  bucketTicks: number,
  opt: DomOptions,
): void {
  const ctx = canvas.getContext("2d")!;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = C.bg;
  ctx.fillRect(0, 0, DOM_W, canvas.height / dpr);
  ctx.font = "11px ui-monospace, monospace";
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, HEADER_H, DOM_W, ph - HEADER_H);
  ctx.clip();
  if (data) {
    const rb = data.rowBuckets;
    // Preis je Zeile (Mitte) für die USDT-Umrechnung
    const conv = (row: DomRow, q: number) =>
      opt.usd ? q * (row.bucket + rb / 2) * bucketTicks * opt.tickSize : q;
    let maxBook = 0;
    let maxVol = 0;
    for (const r of data.rows) {
      maxBook = Math.max(maxBook, conv(r, r.bid), conv(r, r.ask));
      maxVol = Math.max(maxVol, conv(r, r.buy + r.sell));
    }
    const rowPx = (rb * ph) / (view.yHi - view.yLo);
    const showText = rowPx >= 12;
    ctx.textBaseline = "middle";
    for (const r of data.rows) {
      const yTop = unitToY(r.bucket + rb, view, ph);
      const yBot = unitToY(r.bucket, view, ph);
      if (yBot < HEADER_H || yTop > ph) continue;
      const h = Math.max(1, yBot - yTop - 1);
      const yMid = (yTop + yBot) / 2;
      if (r.isBestBid || r.isBestAsk) {
        ctx.fillStyle = r.isBestBid ? C.bestBid : C.bestAsk;
        ctx.fillRect(0, yTop, COLS.vol[0], h);
      }
      const bid = conv(r, r.bid);
      const ask = conv(r, r.ask);
      // Bid-Balken wächst von der Mitte nach links, Ask nach rechts
      if (bid > 0 && maxBook > 0) {
        const w = (bid / maxBook) * (COLS.bid[1] - 2);
        ctx.fillStyle = C.bid;
        ctx.fillRect(COLS.bid[0] + COLS.bid[1] - w, yTop, w, h);
      }
      if (ask > 0 && maxBook > 0) {
        const w = (ask / maxBook) * (COLS.ask[1] - 2);
        ctx.fillStyle = C.ask;
        ctx.fillRect(COLS.ask[0], yTop, w, h);
      }
      const vol = conv(r, r.buy + r.sell);
      if (vol > 0 && maxVol > 0) {
        const w = (vol / maxVol) * (COLS.vol[1] - 4);
        const wBuy = (w * r.buy) / (r.buy + r.sell);
        ctx.fillStyle = C.buy;
        ctx.fillRect(COLS.vol[0] + 2, yTop, wBuy, h);
        ctx.fillStyle = C.sell;
        ctx.fillRect(COLS.vol[0] + 2 + wBuy, yTop, w - wBuy, h);
      }
      if (showText) {
        const big = (v: number) => maxBook > 0 && v >= maxBook * 0.5;
        if (bid > 0) {
          ctx.font = big(bid) ? "bold 11px ui-monospace, monospace" : "11px ui-monospace, monospace";
          ctx.fillStyle = C.bidText;
          const t = fmtQty(bid);
          ctx.fillText(t, COLS.bid[0] + COLS.bid[1] - 4 - ctx.measureText(t).width, yMid);
        }
        if (ask > 0) {
          ctx.font = big(ask) ? "bold 11px ui-monospace, monospace" : "11px ui-monospace, monospace";
          ctx.fillStyle = C.askText;
          ctx.fillText(fmtQty(ask), COLS.ask[0] + 4, yMid);
        }
        ctx.font = "11px ui-monospace, monospace";
        if (vol > 0) {
          ctx.fillStyle = C.text;
          ctx.fillText(fmtQty(vol), COLS.vol[0] + 5, yMid);
        }
        const d = conv(r, r.buy - r.sell);
        if (d !== 0) {
          ctx.fillStyle = d > 0 ? C.bidText : C.askText;
          const t = fmtQty(d);
          ctx.fillText(t, COLS.delta[0] + COLS.delta[1] - 4 - ctx.measureText(t).width, yMid);
        }
      }
    }
    // feine Trennlinien zwischen den Spalten
    ctx.fillStyle = C.grid;
    for (const x of [COLS.ask[0], COLS.vol[0], COLS.delta[0]]) ctx.fillRect(x, HEADER_H, 1, ph - HEADER_H);
  }
  ctx.restore();
  // Kopfzeile
  ctx.fillStyle = C.head;
  ctx.fillRect(0, 0, DOM_W, HEADER_H);
  ctx.fillStyle = C.muted;
  ctx.textBaseline = "middle";
  ctx.font = "10px ui-monospace, monospace";
  const unit = opt.usd ? "$" : "";
  const label = (t: string, [x, w]: readonly [number, number], align: "l" | "r" | "c") => {
    const tw = ctx.measureText(t).width;
    const tx = align === "l" ? x + 4 : align === "r" ? x + w - 4 - tw : x + (w - tw) / 2;
    ctx.fillText(t, tx, HEADER_H / 2);
  };
  const avg = opt.average ? "Ø " : "";
  label(`${avg}BID${unit}`, COLS.bid, "r");
  label(`${avg}ASK${unit}`, COLS.ask, "l");
  if (!opt.average) {
    label(`VOL${unit}`, COLS.vol, "l");
    label("Δ", COLS.delta, "r");
  } else label("Mittel je Spalte", COLS.vol, "l");
  if (!data || data.k === null) {
    ctx.fillStyle = C.muted;
    ctx.fillText("kein Orderbuch an dieser Stelle", 8, HEADER_H + 14);
  }
}
