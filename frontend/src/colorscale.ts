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
 * die hellste Farbe), "Schwelle" ist das Perzentil, unter dem nichts gezeichnet wird – und
 * zugleich der Beginn der Farbskala. Standard ist der Median: Die Hälfte der sichtbaren
 * Level (das typische Grundrauschen) bleibt Hintergrund, Farbe bekommt nur, was über dem
 * Durchschnitt liegt. Darunter wird weich ausgeblendet (Shader), nicht hart abgeschnitten.
 */

/**
 * Paletten auf neutral-grauem Grund. Alle beginnen exakt bei der Hintergrundfarbe, damit wenig
 * Liquidität weich in den Hintergrund ausläuft, und werden mit jeder Stufe *heller*:
 * Helligkeit liest das Auge als "mehr" – große Wände leuchten, Grundrauschen tritt zurück.
 * Ein Farbton, der mehrfach wechselt (die alte "Klassisch"-Palette), liest sich dagegen wie
 * Kategorien und erzeugt Streifenmuster.
 */
export const BACKGROUND: [number, number, number] = [38, 39, 42];

type Stops = [number, [number, number, number]][];

export const PALETTES: Record<string, { label: string; stops: Stops }> = {
  // dunkelblau -> petrol -> grün -> gelb -> weiß (an Viridis angelehnt: gleichmäßige Helligkeit,
  // auch bei Rot-Grün-Schwäche unterscheidbar)
  polar: {
    label: "Polar",
    stops: [
      [0.0, BACKGROUND],
      [0.18, [38, 58, 96]],
      [0.38, [38, 104, 142]],
      [0.56, [40, 156, 152]],
      [0.72, [96, 200, 118]],
      [0.86, [214, 226, 88]],
      [1.0, [255, 250, 214]],
    ],
  },
  // dunkelrot -> orange -> gelb -> weiß (an Inferno angelehnt): wirkt wie Glut
  glut: {
    label: "Glut",
    stops: [
      [0.0, BACKGROUND],
      [0.2, [80, 39, 64]],
      [0.42, [146, 40, 74]],
      [0.6, [210, 72, 48]],
      [0.76, [244, 134, 34]],
      [0.9, [250, 206, 86]],
      [1.0, [255, 250, 220]],
    ],
  },
  // nur Blau -> Weiß: ruhig, Kerzen und Bubbles stechen am stärksten hervor
  eis: {
    label: "Eis",
    stops: [
      [0.0, BACKGROUND],
      [0.25, [40, 60, 100]],
      [0.55, [52, 116, 186]],
      [0.8, [128, 190, 240]],
      [1.0, [240, 250, 255]],
    ],
  },
  grau: {
    label: "Graustufen",
    stops: [
      [0.0, BACKGROUND],
      [1.0, [250, 250, 250]],
    ],
  },
  // die ursprüngliche Palette (Pflaume -> Magenta -> Orange)
  klassisch: {
    label: "Klassisch",
    stops: [
      [0.0, BACKGROUND],
      [0.14, [62, 50, 70]],
      [0.32, [98, 56, 116]],
      [0.5, [150, 62, 140]],
      [0.65, [208, 82, 118]],
      [0.79, [242, 130, 84]],
      [0.91, [255, 198, 106]],
      [1.0, [255, 247, 224]],
    ],
  },
};

export const DEFAULT_PALETTE = "polar";

export function buildLut(palette = DEFAULT_PALETTE, size = 256): Uint8Array {
  const stops = (PALETTES[palette] ?? PALETTES[DEFAULT_PALETTE]!).stops;
  const lut = new Uint8Array(size * 4);
  for (let i = 0; i < size; i++) {
    const t = i / (size - 1);
    let j = 0;
    while (j < stops.length - 2 && t > stops[j + 1]![0]) j++;
    const [t0, c0] = stops[j]!;
    const [t1, c1] = stops[j + 1]!;
    const f = Math.min(1, Math.max(0, (t - t0) / (t1 - t0)));
    for (let k = 0; k < 3; k++) lut[i * 4 + k] = Math.round(c0[k]! + (c1[k]! - c0[k]!) * f);
    lut[i * 4 + 3] = 255;
  }
  return lut;
}

/** Relative Helligkeit (0..1) einer LUT-Stufe – für den Test "Palette wird stetig heller". */
export function luminance(lut: Uint8Array, i: number): number {
  const lin = (c: number) => {
    const v = c / 255;
    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(lut[i * 4]!) + 0.7152 * lin(lut[i * 4 + 1]!) + 0.0722 * lin(lut[i * 4 + 2]!);
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
/** Standard-Schwelle (Perzentil): Median – siehe oben */
export const DEFAULT_THRESHOLD = 0.5;

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
