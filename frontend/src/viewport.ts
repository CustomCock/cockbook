/**
 * Sichtbereich und Koordinaten-Mapping (reine Funktionen).
 *
 * Preis-Einheit ist der Bucket (float). Bucket b deckt die Ticks
 * [b * bucketTicks, (b + 1) * bucketTicks) ab. Ein Preis p (in Ticks) liegt in der
 * Mitte seines Ticks: `priceUnit(p) = (p + 0.5) / bucketTicks` – so landen Trades und
 * BBO-Linien exakt in der Zelle ihres Preises.
 *
 * x: rechte Kante = neueste Spalte (minus `offset` beim Zurückscrollen).
 */

export interface View {
  yLo: number; // unterer Rand in Buckets
  yHi: number; // oberer Rand in Buckets
  colWidth: number; // CSS-px pro Spalte
  offset: number; // Spalten, um die zurückgescrollt ist (0 = live)
  autoCenter: boolean;
}

export const MIN_COL_WIDTH = 0.25;
export const MAX_COL_WIDTH = 24;

export function priceUnit(priceTicks: number, bucketTicks: number): number {
  return (priceTicks + 0.5) / bucketTicks;
}

export function unitToY(u: number, v: View, h: number): number {
  return ((v.yHi - u) / (v.yHi - v.yLo)) * h;
}

export function yToUnit(y: number, v: View, h: number): number {
  return v.yHi - (y / h) * (v.yHi - v.yLo);
}

/** x-Mitte der Spalte k Spalten vor der neuesten. */
export function colToX(k: number, v: View, w: number): number {
  return w - (k - v.offset + 0.5) * v.colWidth;
}

export function xToCol(x: number, v: View, w: number): number {
  return Math.floor((w - x) / v.colWidth) + v.offset;
}

export function visibleCols(v: View, w: number): number {
  return Math.ceil(w / v.colWidth) + 1;
}

export function zoomY(v: View, factor: number, anchor: number): View {
  const span = (v.yHi - v.yLo) * factor;
  const clamped = Math.min(Math.max(span, 8), 20000);
  const f = clamped / (v.yHi - v.yLo);
  return { ...v, yLo: anchor - (anchor - v.yLo) * f, yHi: anchor + (v.yHi - anchor) * f };
}

export function zoomX(v: View, factor: number): View {
  const colWidth = Math.min(Math.max(v.colWidth * factor, MIN_COL_WIDTH), MAX_COL_WIDTH);
  return { ...v, colWidth };
}

export function panY(v: View, deltaUnits: number): View {
  return { ...v, yLo: v.yLo + deltaUnits, yHi: v.yHi + deltaUnits, autoCenter: false };
}

/** Zentriert neu, wenn `mid` näher als `margin` (Anteil der Höhe) am Rand liegt. */
export function recenter(v: View, mid: number, margin: number, force = false): View {
  const span = v.yHi - v.yLo;
  const lo = v.yLo + margin * span;
  const hi = v.yHi - margin * span;
  if (!force && mid >= lo && mid <= hi) return v;
  return { ...v, yLo: mid - span / 2, yHi: mid + span / 2 };
}
