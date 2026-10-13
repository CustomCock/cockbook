/**
 * Geometrie für den Plotbereich (reine Funktionen, CSS-Pixel, Ursprung oben links).
 *
 * Erzeugt Vertex-Daten für den WebGL-Primitive-Renderer: Linien (BBO, Gitter) und
 * Punkte (Trade-Bubbles). Getrennt vom Zeichnen, damit Positionen testbar sind –
 * z. B. dass ein Trade exakt auf der Höhe seines Preises liegt.
 *
 * Vertex-Layout (7 floats): x, y, radius, r, g, b, a
 */

import type { HeatmapStore } from "./store";
import { colToX, priceUnit, unitToY, visibleCols, type View } from "./viewport";

export const STRIDE = 7;

export type RGBA = readonly [number, number, number, number];

export const COLORS = {
  bid: [0.24, 0.86, 0.59, 1] as RGBA,
  ask: [1.0, 0.36, 0.45, 1] as RGBA,
  buy: [0.24, 0.86, 0.59, 0.55] as RGBA,
  sell: [1.0, 0.36, 0.45, 0.55] as RGBA,
  grid: [0.6, 0.64, 0.7, 0.1] as RGBA,
  unsynced: [1.0, 0.36, 0.45, 0.14] as RGBA,
};

export class VertexBuilder {
  data: Float32Array;
  n = 0; // Anzahl Vertices

  constructor(capacity = 4096) {
    this.data = new Float32Array(capacity * STRIDE);
  }

  reset(): void {
    this.n = 0;
  }

  push(x: number, y: number, r: number, c: RGBA): void {
    if ((this.n + 1) * STRIDE > this.data.length) {
      const next = new Float32Array(this.data.length * 2);
      next.set(this.data);
      this.data = next;
    }
    const o = this.n * STRIDE;
    const d = this.data;
    d[o] = x;
    d[o + 1] = y;
    d[o + 2] = r;
    d[o + 3] = c[0];
    d[o + 4] = c[1];
    d[o + 5] = c[2];
    d[o + 6] = c[3];
    this.n++;
  }

  line(x0: number, y0: number, x1: number, y1: number, c: RGBA): void {
    this.push(x0, y0, 0, c);
    this.push(x1, y1, 0, c);
  }

  /** Rechteck als zwei Dreiecke. */
  rect(x: number, y: number, w: number, h: number, c: RGBA): void {
    this.push(x, y, 0, c);
    this.push(x + w, y, 0, c);
    this.push(x, y + h, 0, c);
    this.push(x + w, y, 0, c);
    this.push(x + w, y + h, 0, c);
    this.push(x, y + h, 0, c);
  }
}

export interface SceneParams {
  store: HeatmapStore;
  view: View;
  pw: number;
  ph: number;
  bucketTicks: number;
  gridStepTicks: number;
  bubblePxPerUnit: number;
  bubbleMaxPx: number;
}

export function bubbleRadius(qty: number, pxPerUnit: number, maxPx: number): number {
  // Fläche ~ Volumen -> Radius ~ sqrt(Volumen)
  return Math.min(maxPx, Math.max(1.5, Math.sqrt(qty) * pxPerUnit));
}

/** Dreiecke: Markierung nicht synchroner Spalten. */
export function buildTriangles(p: SceneParams, out: VertexBuilder): void {
  out.reset();
  const { store, view, pw, ph } = p;
  const n = Math.min(visibleCols(view, pw), store.count - view.offset);
  for (let k = view.offset; k < view.offset + n; k++) {
    if (!store.synced[store.indexAgo(k)]) {
      const x = colToX(k, view, pw) - view.colWidth / 2;
      out.rect(x, 0, Math.max(view.colWidth, 1), ph, COLORS.unsynced);
    }
  }
}

/** Linien: Preis-Gitter und BBO als Treppen. */
export function buildLines(p: SceneParams, out: VertexBuilder): void {
  out.reset();
  const { store, view, pw, ph, bucketTicks: bt } = p;
  if (p.gridStepTicks > 0) {
    const first = Math.ceil((view.yLo * bt) / p.gridStepTicks) * p.gridStepTicks;
    for (let t = first; t <= view.yHi * bt; t += p.gridStepTicks) {
      const y = Math.round(unitToY(t / bt, view, ph)) + 0.5;
      out.line(0, y, pw, y, COLORS.grid);
    }
  }
  const n = Math.min(visibleCols(view, pw), store.count - view.offset);
  for (const [arr, color] of [
    [store.bestBid, COLORS.bid],
    [store.bestAsk, COLORS.ask],
  ] as const) {
    let prevY: number | null = null;
    for (let k = view.offset + n - 1; k >= view.offset; k--) {
      const price = arr[store.indexAgo(k)]!;
      if (Number.isNaN(price)) {
        prevY = null;
        continue;
      }
      const x0 = colToX(k, view, pw) - view.colWidth / 2;
      const x1 = x0 + view.colWidth;
      const y = unitToY(priceUnit(price, bt), view, ph);
      if (prevY !== null && prevY !== y) out.line(x0, prevY, x0, y, color); // Sprung
      out.line(x0, y, x1, y, color);
      prevY = y;
    }
  }
}

/** Punkte: Trade-Bubbles (Verkäufe zuerst, Käufe darüber). */
export function buildBubbles(p: SceneParams, out: VertexBuilder): void {
  out.reset();
  const { store, view, pw, ph, bucketTicks: bt } = p;
  const n = Math.min(visibleCols(view, pw), store.count - view.offset);
  for (const side of [-1, 1] as const) {
    const color = side === 1 ? COLORS.buy : COLORS.sell;
    for (let k = view.offset; k < view.offset + n; k++) {
      const trades = store.trades[store.indexAgo(k)];
      if (!trades) continue;
      const x = colToX(k, view, pw);
      for (const t of trades) {
        if (t.side !== side) continue;
        const r = bubbleRadius(t.qty, p.bubblePxPerUnit, p.bubbleMaxPx);
        const y = unitToY(priceUnit(t.vwapTicks, bt), view, ph);
        if (y < -r || y > ph + r) continue;
        out.push(x, y, r, color);
      }
    }
  }
}
