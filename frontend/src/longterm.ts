/**
 * Langzeit-Heatmap: grobe Spalten (z. B. 2 s, je 2 Preis-Buckets zusammengefasst) liegen
 * unter der feinen 100-ms-Heatmap. Beide teilen sich die Sicht (`View`), die in feinen
 * Einheiten rechnet: x in feinen Spalten ab der neuesten feinen Spalte, y in feinen Buckets.
 *
 * Warum zwei Ebenen statt einer langen feinen Historie? 2 h in 100 ms wären 72 000 Spalten
 * x ~2000 Zeilen x 4 Byte = ~570 MB GPU-Speicher. Grob sind 2 h nur 3600 Spalten (~15 MB),
 * und beim Herauszoomen liegen ohnehin viele feine Spalten auf einem Pixel.
 */

import type { View } from "./viewport";

export interface LongParams {
  fineIntervalMs: number;
  longIntervalMs: number;
  factor: number; // feine Buckets je grobem Bucket
}

/**
 * Rechnet die Sicht in grobe Einheiten um.
 *
 * Feine Spalte k (ab der neuesten, Zeit `fineLatest`) liegt bei Zeit fineLatest - k * fI.
 * Grobe Spalte j bei coarseLatest - j * cI. Gleiche Zeit => gleiche x-Position:
 *   j = (k * fI - (fineLatest - coarseLatest)) / cI
 * Daraus folgen Spaltenbreite (x cI/fI) und Offset; y wird durch `factor` geteilt.
 */
export function coarseView(view: View, fineLatest: number | null, coarseLatest: number, p: LongParams): View {
  const ratio = p.longIntervalMs / p.fineIntervalMs;
  const lead = fineLatest === null ? 0 : fineLatest - coarseLatest;
  return {
    yLo: view.yLo / p.factor,
    yHi: view.yHi / p.factor,
    colWidth: view.colWidth * ratio,
    offset: (view.offset * p.fineIntervalMs - lead) / p.longIntervalMs,
    autoCenter: view.autoCenter,
  };
}

/** Grobe Spalte unter der feinen Spalte `k` (kann negativ sein = neuer als die letzte grobe). */
export function coarseColAt(k: number, fineLatest: number, coarseLatest: number, p: LongParams): number {
  return Math.floor((k * p.fineIntervalMs - (fineLatest - coarseLatest)) / p.longIntervalMs);
}
