/**
 * Kerzen: 1m-Basis vom Backend (Binance-Historie + Live-Stream), 3m/5m/10m werden hier
 * daraus gebaut. Binance kennt kein 10m-Intervall – und aus einer gemeinsamen 1m-Basis
 * sind alle Intervalle garantiert konsistent (gleiche Hochs/Tiefs).
 * Ausrichtung auf volle Intervalle in UTC (10:00, 10:10, …), wie bei Börsen üblich.
 */

import type { HeatmapStore } from "./store";
import { unitToY, type View } from "./viewport";

export interface Bar {
  t: number; // Öffnungszeit ms
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export const CANDLE_INTERVALS = [1, 3, 5, 10] as const;
export type CandleMinutes = (typeof CANDLE_INTERVALS)[number];

const MIN_MS = 60_000;

export class CandleSeries {
  private bars = new Map<number, Bar>();
  private sorted: Bar[] | null = null;
  version = 0;

  clear(): void {
    this.bars.clear();
    this.sorted = null;
    this.version++;
  }

  setHistory(rows: number[][]): void {
    for (const r of rows) this.upsert({ t: r[0]!, o: r[1]!, h: r[2]!, l: r[3]!, c: r[4]!, v: r[5]! });
  }

  upsert(b: Bar): void {
    this.bars.set(b.t, b);
    this.sorted = null;
    this.version++;
  }

  all(): Bar[] {
    if (!this.sorted) this.sorted = [...this.bars.values()].sort((a, b) => a.t - b.t);
    return this.sorted;
  }

  get first(): number | null {
    return this.all()[0]?.t ?? null;
  }
}

/** Fasst 1m-Kerzen zu `minutes`-Kerzen zusammen (Eingabe sortiert). */
export function aggregate(bars: Bar[], minutes: number): Bar[] {
  if (minutes <= 1) return bars;
  const p = minutes * MIN_MS;
  const out: Bar[] = [];
  let cur: Bar | null = null;
  for (const b of bars) {
    const t = Math.floor(b.t / p) * p;
    if (!cur || cur.t !== t) {
      cur = { t, o: b.o, h: b.h, l: b.l, c: b.c, v: b.v };
      out.push(cur);
    } else {
      cur.h = Math.max(cur.h, b.h);
      cur.l = Math.min(cur.l, b.l);
      cur.c = b.c;
      cur.v += b.v;
    }
  }
  return out;
}

// --- Geometrie für den Rounded-Rect-Renderer --------------------------------------------
// Vertex: x, y, localX, localY, halfW, halfH, radius, r, g, b, a  (11 floats)

export const RRECT_STRIDE = 11;

export class RRectBuilder {
  data = new Float32Array(1024 * RRECT_STRIDE);
  n = 0;

  reset(): void {
    this.n = 0;
  }

  /** Abgerundetes Rechteck (CSS-px), Mittelpunkt cx/cy, halbe Breite/Höhe, Eckradius. */
  rect(cx: number, cy: number, hw: number, hh: number, radius: number, c: readonly number[]): void {
    const pad = 1; // Rand für Kantenglättung
    const ex = hw + pad;
    const ey = hh + pad;
    if ((this.n + 6) * RRECT_STRIDE > this.data.length) {
      const next = new Float32Array(this.data.length * 2);
      next.set(this.data);
      this.data = next;
    }
    for (const [sx, sy] of [
      [-1, -1],
      [1, -1],
      [-1, 1],
      [1, -1],
      [1, 1],
      [-1, 1],
    ] as const) {
      const o = this.n * RRECT_STRIDE;
      const d = this.data;
      d[o] = cx + sx * ex;
      d[o + 1] = cy + sy * ey;
      d[o + 2] = sx * ex;
      d[o + 3] = sy * ey;
      d[o + 4] = hw;
      d[o + 5] = hh;
      d[o + 6] = radius;
      d[o + 7] = c[0]!;
      d[o + 8] = c[1]!;
      d[o + 9] = c[2]!;
      d[o + 10] = c[3]!;
      this.n++;
    }
  }
}

export const CANDLE_COLORS = {
  up: [0.33, 0.9, 0.66, 0.9],
  down: [1.0, 0.45, 0.52, 0.9],
} as const;

export interface CandleScene {
  store: HeatmapStore;
  view: View;
  pw: number;
  ph: number;
  intervalMs: number; // Heatmap-Spaltenbreite in ms (Zeitachse)
  tickSize: number;
  bucketTicks: number;
}

/** x-Position eines Zeitpunkts – dieselbe Zeitachse wie die Heatmap-Spalten. */
export function timeToX(t: number, s: CandleScene): number {
  const latest = s.store.ts[s.store.latest]!;
  return s.pw - ((latest - t) / s.intervalMs - s.view.offset) * s.view.colWidth;
}

export function priceToY(price: number, s: CandleScene): number {
  return unitToY(price / s.tickSize / s.bucketTicks, s.view, s.ph);
}

/** Kerzen als abgerundete Rechtecke: Docht (schmal) + Körper. */
export function buildCandles(bars: Bar[], minutes: number, s: CandleScene, out: RRectBuilder): void {
  out.reset();
  if (s.store.count === 0) return;
  const p = minutes * MIN_MS;
  for (const b of bars) {
    const x0 = timeToX(b.t, s);
    const x1 = timeToX(b.t + p, s);
    if (x1 < 0 || x0 > s.pw) continue;
    const slot = x1 - x0;
    const cx = (x0 + x1) / 2;
    const color = b.c >= b.o ? CANDLE_COLORS.up : CANDLE_COLORS.down;
    const yH = priceToY(b.h, s);
    const yL = priceToY(b.l, s);
    const yO = priceToY(b.o, s);
    const yC = priceToY(b.c, s);
    const bodyHW = Math.max(0.75, slot * 0.36);
    const wickHW = Math.max(0.5, Math.min(1.25, bodyHW * 0.2));
    out.rect(cx, (yH + yL) / 2, wickHW, Math.max(wickHW, (yL - yH) / 2), wickHW, color);
    const bodyHH = Math.max(0.75, Math.abs(yC - yO) / 2);
    const radius = Math.min(4, bodyHW * 0.45, bodyHH);
    out.rect(cx, (yO + yC) / 2, bodyHW, bodyHH, radius, color);
  }
}
