/**
 * Geometrie für den Plotbereich (reine Funktionen, CSS-Pixel, Ursprung oben links).
 *
 * Erzeugt Vertex-Daten für den WebGL-Primitive-Renderer: weiche Linien (BBO, Gitter),
 * Flächen (nicht synchrone Spalten) und Punkte (Trade-Bubbles, Liquidationen). Getrennt
 * vom Zeichnen, damit Positionen testbar sind – z. B. dass ein Trade exakt auf der
 * Höhe seines Preises liegt.
 *
 * Vertex-Layout (7 floats): x, y, aux, r, g, b, a
 * aux = Radius (Punkte) bzw. Abstand zur Linienmitte (weiche Linien).
 *
 * Weiche Linien als Bänder statt GL_LINES: GL-Linien sind 1 Pixel breit und ohne
 * Kantenglättung (Treppenstufen). Ein Band, dessen Alpha zur Kante hin ausläuft, sieht
 * auf jedem Bildschirm weich aus und funktioniert auch auf Software-Renderern.
 */

import type { HeatmapStore } from "./store";
import { colToX, priceUnit, unitToY, visibleCols, type View } from "./viewport";

export const STRIDE = 7;

export type RGBA = readonly [number, number, number, number];

export const COLORS = {
  bid: [0.33, 0.9, 0.66, 0.95] as RGBA,
  ask: [1.0, 0.45, 0.52, 0.95] as RGBA,
  buy: [0.33, 0.9, 0.66, 0.5] as RGBA,
  sell: [1.0, 0.45, 0.52, 0.5] as RGBA,
  grid: [0.85, 0.88, 0.92, 0.07] as RGBA,
  unsynced: [1.0, 0.45, 0.52, 0.12] as RGBA,
  longLiq: [1.0, 0.66, 0.2, 0.85] as RGBA, // Long liquidiert (Zwangsverkauf)
  shortLiq: [0.35, 0.8, 1.0, 0.85] as RGBA, // Short liquidiert (Zwangskauf)
};

/** Gesamte Breite eines weichen Linienbands = Kern + Auslauf auf beiden Seiten. */
export const LINE = {
  bbo: { half: 0.7, feather: 1.3 },
  grid: { half: 0.3, feather: 0.9 },
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

  push(x: number, y: number, aux: number, c: RGBA): void {
    if ((this.n + 1) * STRIDE > this.data.length) {
      const next = new Float32Array(this.data.length * 2);
      next.set(this.data);
      this.data = next;
    }
    const o = this.n * STRIDE;
    const d = this.data;
    d[o] = x;
    d[o + 1] = y;
    d[o + 2] = aux;
    d[o + 3] = c[0];
    d[o + 4] = c[1];
    d[o + 5] = c[2];
    d[o + 6] = c[3];
    this.n++;
  }

  /** Rechteck als zwei Dreiecke; aux0/aux1 = aux-Wert an oberer/unterer bzw. linker/rechter Kante. */
  private quad(x0: number, y0: number, x1: number, y1: number, c: RGBA, vertical: boolean, e: number): void {
    // aux läuft quer zur Linie von -e bis +e
    const a = (x: number, y: number) => (vertical ? (x === x0 ? -e : e) : y === y0 ? -e : e);
    for (const [x, y] of [
      [x0, y0],
      [x1, y0],
      [x0, y1],
      [x1, y0],
      [x1, y1],
      [x0, y1],
    ] as const) {
      this.push(x, y, a(x, y), c);
    }
  }

  rect(x: number, y: number, w: number, h: number, c: RGBA): void {
    this.quad(x, y, x + w, y + h, c, false, 0);
  }

  /** Waagerechte weiche Linie; `extent` = halbe Gesamtbreite (Kern + Auslauf). */
  hLine(x0: number, x1: number, y: number, extent: number, c: RGBA): void {
    this.quad(x0, y - extent, x1, y + extent, c, false, extent);
  }

  /** Senkrechte weiche Linie. */
  vLine(x: number, y0: number, y1: number, extent: number, c: RGBA): void {
    this.quad(x - extent, Math.min(y0, y1), x + extent, Math.max(y0, y1), c, true, extent);
  }
}

export interface SceneParams {
  store: HeatmapStore;
  view: View;
  pw: number;
  ph: number;
  bucketTicks: number;
  tickSize: number;
  gridStepTicks: number;
  /** Bubble-Radius in px pro Wurzel(Gegenwert in 1000 USDT) */
  bubblePxPerSqrtKusd: number;
  bubbleMaxPx: number;
  /** Nur Spalten ab dieser (älter); für die Langzeit-Ebene hinter der feinen Heatmap */
  kMin?: number;
}

/**
 * Fläche ~ Gegenwert in USDT -> Radius ~ Wurzel(Gegenwert). Bewusst USDT statt
 * Basis-Menge: 1 BTC und 1 XRP unterscheiden sich um den Faktor ~40 000, in USDT sind
 * Bubbles über alle Coins vergleichbar.
 */
export function bubbleRadius(notionalUsd: number, pxPerSqrtKusd: number, maxPx: number): number {
  return Math.min(maxPx, Math.max(1.5, Math.sqrt(notionalUsd / 1000) * pxPerSqrtKusd));
}

function visible(p: SceneParams): number {
  return Math.min(visibleCols(p.view, p.pw), p.store.count - p.view.offset);
}

/** Flächen: Markierung nicht synchroner Spalten. */
export function buildUnsynced(p: SceneParams, out: VertexBuilder): void {
  out.reset();
  const { store, view, pw, ph } = p;
  const n = visible(p);
  for (let k = view.offset; k < view.offset + n; k++) {
    if (!store.synced[store.indexAgo(k)]) {
      const x = colToX(k, view, pw) - view.colWidth / 2;
      out.rect(x, 0, Math.max(view.colWidth, 1), ph, COLORS.unsynced);
    }
  }
}

/** Weiche Linien: Preis-Gitter. */
export function buildGrid(p: SceneParams, out: VertexBuilder): void {
  out.reset();
  const { view, pw, ph, bucketTicks: bt } = p;
  if (p.gridStepTicks <= 0) return;
  const e = LINE.grid.half + LINE.grid.feather;
  const first = Math.ceil((view.yLo * bt) / p.gridStepTicks) * p.gridStepTicks;
  for (let t = first; t <= view.yHi * bt; t += p.gridStepTicks) {
    out.hLine(0, pw, unitToY(t / bt, view, ph), e, COLORS.grid);
  }
}

/** Weiche Linien: BBO als Treppen. */
export function buildBbo(p: SceneParams, out: VertexBuilder): void {
  out.reset();
  const { store, view, pw, ph, bucketTicks: bt } = p;
  const e = LINE.bbo.half + LINE.bbo.feather;
  // offset kann gebrochen sein (Langzeit-Ebene) -> ganze Spalten ab der angeschnittenen
  const kFirst = Math.max(Math.floor(view.offset), Math.ceil(p.kMin ?? 0), 0);
  const kLast = Math.min(Math.ceil(view.offset + visibleCols(view, pw)), store.count - 1);
  for (const [arr, color] of [
    [store.bestBid, COLORS.bid],
    [store.bestAsk, COLORS.ask],
  ] as const) {
    let prevY: number | null = null;
    for (let k = kLast; k >= kFirst; k--) {
      const price = arr[store.indexAgo(k)]!;
      if (Number.isNaN(price)) {
        prevY = null;
        continue;
      }
      const x0 = colToX(k, view, pw) - view.colWidth / 2;
      const y = unitToY(priceUnit(price, bt), view, ph);
      if (prevY !== null && Math.abs(prevY - y) > 0.01) out.vLine(x0, prevY, y, e, color); // Sprung
      out.hLine(x0, x0 + view.colWidth, y, e, color);
      prevY = y;
    }
  }
}

/** Punkte: Trade-Bubbles (Verkäufe zuerst, Käufe darüber). */
export function buildBubbles(p: SceneParams, out: VertexBuilder): void {
  out.reset();
  const { store, view, pw, ph, bucketTicks: bt } = p;
  const n = visible(p);
  for (const side of [-1, 1] as const) {
    const color = side === 1 ? COLORS.buy : COLORS.sell;
    for (let k = view.offset; k < view.offset + n; k++) {
      const trades = store.trades[store.indexAgo(k)];
      if (!trades) continue;
      const x = colToX(k, view, pw);
      for (const t of trades) {
        if (t.side !== side) continue;
        const r = bubbleRadius(t.qty * t.vwapTicks * p.tickSize, p.bubblePxPerSqrtKusd, p.bubbleMaxPx);
        const y = unitToY(priceUnit(t.vwapTicks, bt), view, ph);
        if (y < -r || y > ph + r) continue;
        out.push(x, y, r, color);
      }
    }
  }
}

/** Punkte (Rauten): Liquidationen, Größe nach Gegenwert. */
export function buildLiquidations(p: SceneParams, out: VertexBuilder): void {
  out.reset();
  const { store, view, pw, ph, bucketTicks: bt } = p;
  const n = visible(p);
  for (let k = view.offset; k < view.offset + n; k++) {
    const liqs = store.liqs[store.indexAgo(k)];
    if (!liqs) continue;
    const x = colToX(k, view, pw);
    for (const l of liqs) {
      const r = bubbleRadius(l.qty * l.priceTicks * p.tickSize, p.bubblePxPerSqrtKusd, p.bubbleMaxPx) + 2;
      const y = unitToY(priceUnit(l.priceTicks, bt), view, ph);
      if (y < -r || y > ph + r) continue;
      out.push(x, y, r, l.side === -1 ? COLORS.longLiq : COLORS.shortLiq);
    }
  }
}
