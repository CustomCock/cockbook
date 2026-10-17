/**
 * Preis- und Zeitachse als zwei kleine Canvas2D-Flächen neben dem Plot.
 * Text bleibt in Canvas2D (gestochen scharf, einfach); die Flächen sind schmal,
 * daher auch ohne GPU billig.
 */

import type { HeatmapStore } from "./store";
import { colToX, priceUnit, unitToY, type View } from "./viewport";

export interface Instrument {
  symbol: string;
  tickSize: number;
  tickDecimals: number;
  bucketTicks: number;
}

export const AXIS_W = 84;
export const AXIS_H = 24;

const BG = "#222326";
const TEXT = "#a9adb3";
const BID = "#55e5a8";
const ASK = "#ff7385";

export function formatPrice(ticks: number, inst: Instrument): string {
  return (ticks * inst.tickSize).toFixed(inst.tickDecimals);
}

export function niceStep(raw: number): number {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= raw) return m * p;
  return 10 * p;
}

/** Gitterabstand in Ticks, sodass Linien ~60 px auseinander liegen. */
export function gridStepTicks(view: View, ph: number, inst: Instrument): number {
  const unitsPerPx = (view.yHi - view.yLo) / ph;
  return niceStep(unitsPerPx * 60 * inst.bucketTicks * inst.tickSize) / inst.tickSize;
}

/** Übliche Zeit-Schritte in Sekunden (statt 1-2-5-Folge, die krumme Uhrzeiten ergäbe). */
const TIME_STEPS = [
  0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200,
  86400, 172800, 345600, 604800, 1209600,
];

export function niceTimeStep(rawSec: number): number {
  return TIME_STEPS.find((s) => s >= rawSec) ?? 1209600;
}

/** Datum "16.10." (lokale Zeit). */
export function dateLabel(ms: number): string {
  const d = new Date(ms);
  return `${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")}.`;
}

export function timeLabel(ms: number, withMs: boolean, withSec = true): string {
  const d = new Date(ms);
  const p2 = (n: number) => String(n).padStart(2, "0");
  if (!withSec) return `${p2(d.getHours())}:${p2(d.getMinutes())}`;
  const base = `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
  return withMs ? `${base}.${String(d.getMilliseconds()).padStart(3, "0")}` : base;
}

function prepare(canvas: HTMLCanvasElement, dpr: number): CanvasRenderingContext2D {
  const ctx = canvas.getContext("2d")!;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = BG;
  ctx.fillRect(0, 0, canvas.width / dpr, canvas.height / dpr);
  ctx.font = "11px ui-monospace, monospace";
  return ctx;
}

export function drawPriceAxis(
  canvas: HTMLCanvasElement,
  dpr: number,
  store: HeatmapStore | null,
  view: View,
  inst: Instrument | null,
  ph: number,
): void {
  const ctx = prepare(canvas, dpr);
  if (!inst) return;
  const bt = inst.bucketTicks;
  const step = gridStepTicks(view, ph, inst);
  ctx.fillStyle = TEXT;
  ctx.textBaseline = "middle";
  for (let t = Math.ceil((view.yLo * bt) / step) * step; t <= view.yHi * bt; t += step) {
    const y = unitToY(t / bt, view, ph);
    if (y > 6 && y < ph - 6) ctx.fillText(formatPrice(Math.round(t), inst), 6, y);
  }
  if (!store || store.count === 0 || view.offset > 0) return;
  // Aktuelle BBO-Marken; bei engem Spread auseinanderschieben statt überdecken
  const i = store.latest;
  const bb = store.bestBid[i]!;
  const ba = store.bestAsk[i]!;
  if (Number.isNaN(bb) || Number.isNaN(ba)) return;
  let yAsk = unitToY(priceUnit(ba, bt), view, ph);
  let yBid = unitToY(priceUnit(bb, bt), view, ph);
  const minGap = 17;
  if (yBid - yAsk < minGap) {
    const mid = (yAsk + yBid) / 2;
    yAsk = mid - minGap / 2;
    yBid = mid + minGap / 2;
  }
  for (const [price, y, color] of [
    [ba, yAsk, ASK],
    [bb, yBid, BID],
  ] as const) {
    if (y < -8 || y > ph + 8) continue;
    ctx.fillStyle = color;
    ctx.fillRect(1, y - 8, AXIS_W - 2, 16);
    ctx.fillStyle = "#1d2026";
    ctx.fillText(formatPrice(price, inst), 6, y);
  }
}

/**
 * `latestTs`: Zeit der neuesten Spalte (x-Bezug), null = keine Daten.
 * `dates`: Langzeit-Ansicht – Mitternacht und Schritte ab einem Tag als Datum beschriften.
 */
export function drawTimeAxis(
  canvas: HTMLCanvasElement,
  dpr: number,
  latestTs: number | null,
  view: View,
  intervalMs: number,
  pw: number,
  dates = false,
): void {
  const ctx = prepare(canvas, dpr);
  if (latestTs === null) return;
  ctx.fillStyle = TEXT;
  ctx.textBaseline = "top";
  const pxPerSec = (view.colWidth * 1000) / intervalMs;
  const stepSec = niceTimeStep(110 / pxPerSec);
  // Aus der Zeit rechnen statt aus dem Ringpuffer: zurückgescrollt über die feine
  // Historie hinaus (Langzeit-Ebene, Kerzen) gibt es dort keine Spalte mehr.
  const newestTs = latestTs - view.offset * intervalMs;
  // Auf lokale Mitternacht ausrichten, damit Tages-Schritte auf 00:00 fallen
  const tz = new Date(newestTs).getTimezoneOffset() * 60_000;
  const first = Math.floor((newestTs - tz) / 1000 / stepSec) * stepSec * 1000 + tz;
  for (let ts = first; ; ts -= stepSec * 1000) {
    const x = colToX(view.offset + (newestTs - ts) / intervalMs, view, pw);
    if (x < 30) break;
    if (x < pw - 30) {
      const d = new Date(ts);
      const midnight = d.getHours() === 0 && d.getMinutes() === 0;
      const label =
        dates && (stepSec >= 86400 || midnight) ? dateLabel(ts) : timeLabel(ts, stepSec < 1, stepSec < 60);
      ctx.fillText(label, x - ctx.measureText(label).width / 2, 6);
    }
  }
}
