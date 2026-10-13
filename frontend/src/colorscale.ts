/**
 * Farbskala: logarithmisch, verankert an Perzentilen der sichtbaren Liquidität.
 *
 * Warum log + Perzentil (und nicht nur eins von beiden)?
 * - Liquidität ist extrem schief verteilt: eine Wall hat oft das 100- bis 1000-fache
 *   eines normalen Levels. Linear wäre fast alles schwarz und nur die Walls hell.
 *   Log staucht das, und Verhältnisse bleiben sichtbar (2x mehr = gleicher Farbschritt,
 *   egal ob 1 -> 2 BTC oder 50 -> 100 BTC). Das entspricht auch der Wahrnehmung.
 * - *Welche* Werte auf "dunkel" und "hell" fallen, hängt aber vom Markt ab (BTC vs.
 *   ein Altcoin, ruhige vs. hektische Phase). Feste Grenzen wären mal überstrahlt, mal
 *   zu dunkel. Deshalb Ober-/Untergrenze aus Perzentilen der aktuell sichtbaren Werte.
 * - Reines Perzentil-/Rang-Mapping (Histogrammausgleich) wäre maximal kontrastreich,
 *   zerstört aber die Größenverhältnisse: eine 10x-Wall und eine 1000x-Wall sähen
 *   gleich aus. Darum nur als Anker, nicht als Abbildung.
 *
 * Regler: "Kontrast" teilt die Obergrenze (mehr Kontrast -> mehr Level erreichen
 * die hellste Farbe), "Schwelle" ist das Perzentil, unter dem nichts gezeichnet wird.
 */

/** Eigene Palette (dunkel -> Indigo -> Violett -> Magenta -> Orange -> Bernstein -> Hell). */
const STOPS: [number, [number, number, number]][] = [
  [0.0, [14, 16, 30]],
  [0.15, [34, 26, 82]],
  [0.32, [72, 44, 140]],
  [0.5, [134, 52, 150]],
  [0.66, [200, 70, 112]],
  [0.8, [242, 124, 74]],
  [0.92, [255, 200, 98]],
  [1.0, [255, 248, 222]],
];

export const BACKGROUND: [number, number, number] = [11, 14, 19];

export function buildLut(size = 256): Uint8Array {
  const lut = new Uint8Array(size * 4);
  for (let i = 0; i < size; i++) {
    const t = i / (size - 1);
    let j = 0;
    while (j < STOPS.length - 2 && t > STOPS[j + 1]![0]) j++;
    const [t0, c0] = STOPS[j]!;
    const [t1, c1] = STOPS[j + 1]!;
    const f = Math.min(1, Math.max(0, (t - t0) / (t1 - t0)));
    for (let k = 0; k < 3; k++) lut[i * 4 + k] = Math.round(c0[k]! + (c1[k]! - c0[k]!) * f);
    lut[i * 4 + 3] = 255;
  }
  return lut;
}

/** p in [0, 1]; `sorted` aufsteigend. Lineare Interpolation zwischen Rängen. */
export function percentile(sorted: ArrayLike<number>, p: number): number {
  const n = sorted.length;
  if (n === 0) return NaN;
  const pos = Math.min(Math.max(p, 0), 1) * (n - 1);
  const i = Math.floor(pos);
  const f = pos - i;
  const a = sorted[i]!;
  const b = sorted[Math.min(i + 1, n - 1)]!;
  return a + (b - a) * f;
}

export interface ScaleSettings {
  contrast: number; // 0.25 .. 8, Default 1
  threshold: number; // Perzentil 0 .. 0.9
}

export interface ScaleRange {
  threshold: number; // Werte <= threshold werden nicht gezeichnet
  logLo: number;
  logHi: number;
}

export const UPPER_PERCENTILE = 0.995;

/** Berechnet die Skala aus einer Stichprobe positiver Werte. */
export function computeRange(samples: Float32Array, s: ScaleSettings): ScaleRange | null {
  if (samples.length < 8) return null;
  const sorted = samples.slice().sort();
  const thr = s.threshold > 0 ? percentile(sorted, s.threshold) : 0;
  const lo = Math.max(percentile(sorted, Math.max(s.threshold, 0.02)), 1e-12);
  const hi = Math.max(percentile(sorted, UPPER_PERCENTILE) / s.contrast, lo * 1.01);
  return { threshold: thr, logLo: Math.log(lo), logHi: Math.log(hi) };
}

/** Normierter Farbwert 0..1 für eine Menge (für Legende/Tooltip, gleiche Formel wie Shader). */
export function normalize(v: number, r: ScaleRange): number {
  if (v <= r.threshold || v <= 0) return -1;
  return Math.min(1, Math.max(0, (Math.log(v) - r.logLo) / (r.logHi - r.logLo)));
}
