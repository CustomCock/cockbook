/**
 * Geometrie aller Zeichenwerkzeuge: Ankerpunkte (Zeit/Preis) -> Grundformen in Pixeln.
 *
 * Jedes Werkzeug liefert nur drei Arten von Grundformen: Linienzüge, Flächen und Texte.
 * Kreise, Bögen und Kurven werden dafür in kurze Strecken zerlegt. Derselbe Satz Grundformen
 * wird gezeichnet *und* fürs Anklicken benutzt – was man sieht, kann man auch greifen.
 */

import type { Bar } from "../candles";
import { FIB_LEVELS, type Drawing, type Pt } from "./model";

export type XY = [number, number];

export interface Prim {
  kind: "line" | "poly" | "text";
  pts?: XY[];
  closed?: boolean;
  /** Füllfarbe (Flächen) */
  fill?: string | null;
  /** Strichfarbe; null = kein Rand */
  color?: string | null;
  width?: number;
  dash?: "solid" | "dashed" | "dotted";
  alpha?: number;
  // Text
  x?: number;
  y?: number;
  text?: string;
  align?: CanvasTextAlign;
  base?: CanvasTextBaseline;
  size?: number;
  bg?: string | null;
  /** Text gehört nicht zur Klickfläche (z. B. Achsenbeschriftung) */
  noHit?: boolean;
}

/** Umrechnung und Daten, die die Geometrie braucht (aus dem Chart). */
export interface Ctx {
  toX(t: number): number;
  toY(p: number): number;
  toT(x: number): number;
  toP(y: number): number;
  pw: number;
  ph: number;
  /** Zeit je Chartspalte (ms) */
  interval: number;
  /** 1m-Kerzen (aufsteigend) */
  bars: Bar[];
  /** Trades (aus der Heatmap-Historie) zwischen t0 und t1: Preis, Menge */
  trades(t0: number, t1: number): { p: number; q: number }[];
  fmtP(p: number): string;
  fmtT(t: number): string;
  tick: number;
}

const BIG = 1e5;
const LEVEL_COLORS = ["#787b86", "#f23645", "#ff9800", "#4caf50", "#089981", "#00bcd4", "#787b86", "#2962ff", "#e91e63", "#9c27b0", "#f23645"];

function pct(a: number, b: number): string {
  return `${b >= a ? "+" : ""}${(((b - a) / a) * 100).toFixed(2)} %`;
}

function dur(ms: number): string {
  const m = Math.round(Math.abs(ms) / 60000);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${m % 60} min`;
  return `${Math.floor(h / 24)} T ${h % 24} h`;
}

/** Strecke a-b ggf. über die Punkte hinaus verlängern (auf "unendlich"). */
export function extendSeg(a: XY, b: XY, left: boolean, right: boolean): [XY, XY] {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;
  return [left ? [a[0] - ux * BIG, a[1] - uy * BIG] : a, right ? [b[0] + ux * BIG, b[1] + uy * BIG] : b];
}

/** Kreis/Ellipse/Bogen als Punktfolge. */
export function arcPts(cx: number, cy: number, rx: number, ry: number, a0 = 0, a1 = Math.PI * 2, n = 72): XY[] {
  const out: XY[] = [];
  for (let i = 0; i <= n; i++) {
    const a = a0 + ((a1 - a0) * i) / n;
    out.push([cx + Math.cos(a) * rx, cy + Math.sin(a) * ry]);
  }
  return out;
}

export function quadPts(a: XY, c: XY, b: XY, n = 40): XY[] {
  const out: XY[] = [];
  for (let i = 0; i <= n; i++) {
    const t = i / n;
    const u = 1 - t;
    out.push([u * u * a[0] + 2 * u * t * c[0] + t * t * b[0], u * u * a[1] + 2 * u * t * c[1] + t * t * b[1]]);
  }
  return out;
}

export function cubicPts(a: XY, c1: XY, c2: XY, b: XY, n = 50): XY[] {
  const out: XY[] = [];
  for (let i = 0; i <= n; i++) {
    const t = i / n;
    const u = 1 - t;
    out.push([
      u * u * u * a[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t * t * t * b[0],
      u * u * u * a[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t * t * t * b[1],
    ]);
  }
  return out;
}

/** Kreis durch drei Punkte (null bei kollinear). */
export function circleThrough(a: XY, b: XY, c: XY): { cx: number; cy: number; r: number } | null {
  const d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]));
  if (Math.abs(d) < 1e-9) return null;
  const s = (p: XY) => p[0] * p[0] + p[1] * p[1];
  const cx = (s(a) * (b[1] - c[1]) + s(b) * (c[1] - a[1]) + s(c) * (a[1] - b[1])) / d;
  const cy = (s(a) * (c[0] - b[0]) + s(b) * (a[0] - c[0]) + s(c) * (b[0] - a[0])) / d;
  return { cx, cy, r: Math.hypot(a[0] - cx, a[1] - cy) };
}

/** Lineare Regression (Schlusskurse) über [t0, t1]: Gerade + Standardabweichung. */
export function regression(bars: Bar[], t0: number, t1: number): { a: number; b: number; sd: number; n: number } | null {
  const lo = Math.min(t0, t1);
  const hi = Math.max(t0, t1);
  const xs: number[] = [];
  const ys: number[] = [];
  for (const b of bars) {
    if (b.t >= lo && b.t <= hi) {
      xs.push(b.t);
      ys.push(b.c);
    }
  }
  const n = xs.length;
  if (n < 2) return null;
  const mx = xs.reduce((s, v) => s + v, 0) / n;
  const my = ys.reduce((s, v) => s + v, 0) / n;
  let sxy = 0;
  let sxx = 0;
  for (let i = 0; i < n; i++) {
    sxy += (xs[i]! - mx) * (ys[i]! - my);
    sxx += (xs[i]! - mx) ** 2;
  }
  const b = sxx ? sxy / sxx : 0;
  const a = my - b * mx;
  let ss = 0;
  for (let i = 0; i < n; i++) ss += (ys[i]! - (a + b * xs[i]!)) ** 2;
  return { a, b, sd: Math.sqrt(ss / n), n };
}

/** Kumulierter VWAP ab t0 aus 1m-Kerzen (typischer Preis x Volumen). */
export function anchoredVwap(bars: Bar[], t0: number): { t: number; p: number }[] {
  let pv = 0;
  let v = 0;
  const out: { t: number; p: number }[] = [];
  for (const b of bars) {
    if (b.t < t0) continue;
    pv += ((b.h + b.l + b.c) / 3) * b.v;
    v += b.v;
    if (v > 0) out.push({ t: b.t + 60_000, p: pv / v });
  }
  return out;
}

/**
 * Volumen je Preiszeile im Zeitraum. Mit Trades (Heatmap-Historie) exakt; sonst aus
 * 1m-Kerzen geschätzt (Volumen gleichmäßig über die Spanne der Kerze verteilt).
 */
export function volumeProfile(
  ctx: Ctx,
  t0: number,
  t1: number,
  rows = 40,
): { lo: number; step: number; vol: number[]; exact: boolean; poc: number; vaLo: number; vaHi: number } | null {
  const lo0 = Math.min(t0, t1);
  const hi0 = Math.max(t0, t1);
  const trades = ctx.trades(lo0, hi0);
  const bars = ctx.bars.filter((b) => b.t + 60_000 > lo0 && b.t < hi0);
  const exact = trades.length > 0 && bars.length < 2;
  let pMin = Infinity;
  let pMax = -Infinity;
  if (exact) for (const tr of trades) (pMin = Math.min(pMin, tr.p)), (pMax = Math.max(pMax, tr.p));
  else for (const b of bars) (pMin = Math.min(pMin, b.l)), (pMax = Math.max(pMax, b.h));
  if (!Number.isFinite(pMin) || pMax <= pMin) return null;
  const step = (pMax - pMin) / rows;
  const vol = new Array<number>(rows).fill(0);
  if (exact) {
    for (const tr of trades) vol[Math.min(rows - 1, Math.floor((tr.p - pMin) / step))]! += tr.q;
  } else {
    for (const b of bars) {
      const r0 = Math.max(0, Math.floor((b.l - pMin) / step));
      const r1 = Math.min(rows - 1, Math.floor((b.h - pMin) / step));
      const share = b.v / (r1 - r0 + 1);
      for (let r = r0; r <= r1; r++) vol[r]! += share;
    }
  }
  // Point of Control + 70-%-Value-Area (von der POC aus nach oben/unten erweitern)
  let poc = 0;
  for (let r = 1; r < rows; r++) if (vol[r]! > vol[poc]!) poc = r;
  const total = vol.reduce((s, v) => s + v, 0);
  let lo = poc;
  let hi = poc;
  let acc = vol[poc]!;
  while (acc < total * 0.7 && (lo > 0 || hi < rows - 1)) {
    const down = lo > 0 ? vol[lo - 1]! : -1;
    const up = hi < rows - 1 ? vol[hi + 1]! : -1;
    if (up >= down) acc += vol[++hi]!;
    else acc += vol[--lo]!;
  }
  return { lo: pMin, step, vol, exact, poc, vaLo: lo, vaHi: hi };
}

/** Labels der Mustertools (je Ankerpunkt). */
const PATTERN_LABELS: Record<string, string[]> = {
  xabcd: ["X", "A", "B", "C", "D"],
  cypher: ["X", "A", "B", "C", "D"],
  abcd: ["A", "B", "C", "D"],
  trianglepattern: ["A", "B", "C", "D"],
  headshoulders: ["", "Linke Schulter", "", "Kopf", "", "Rechte Schulter", ""],
  threedrives: ["", "Drive 1", "", "Drive 2", "", "Drive 3", ""],
  elliottimpulse: ["(0)", "(1)", "(2)", "(3)", "(4)", "(5)"],
  elliottcorrection: ["(0)", "(A)", "(B)", "(C)"],
  elliotttriangle: ["(0)", "(A)", "(B)", "(C)", "(D)", "(E)"],
  elliottdouble: ["(0)", "(W)", "(X)", "(Y)"],
  elliotttriple: ["(0)", "(W)", "(X)", "(Y)", "(X)", "(Z)"],
};

function ratio(a: Pt, b: Pt, c: Pt, d: Pt): string {
  const r = Math.abs(d.p - c.p) / (Math.abs(b.p - a.p) || 1);
  return r.toFixed(3);
}

/** Grundformen einer Zeichnung. `pts` kann beim Zeichnen noch unvollständig sein. */
export function build(d: Drawing, ctx: Ctx): Prim[] {
  const s = d.style;
  const P = d.pts;
  const xy = (q: Pt): XY => [ctx.toX(q.t), ctx.toY(q.p)];
  const out: Prim[] = [];
  const line = (pts: XY[], extra: Partial<Prim> = {}) =>
    out.push({ kind: "line", pts, color: s.color, width: s.width, dash: s.dash, ...extra });
  const poly = (pts: XY[], fill: string | null, extra: Partial<Prim> = {}) =>
    out.push({ kind: "poly", pts, closed: true, fill, color: s.color, width: s.width, dash: s.dash, ...extra });
  const text = (x: number, y: number, t: string, extra: Partial<Prim> = {}) =>
    out.push({ kind: "text", x, y, text: t, color: s.color, size: s.fontSize, align: "left", base: "middle", ...extra });
  const seg = (a: XY, b: XY, left = s.extendLeft, right = s.extendRight, extra: Partial<Prim> = {}) =>
    line(extendSeg(a, b, left, right), extra);
  if (!P.length) return out;
  const A = xy(P[0]!);
  const B = P[1] ? xy(P[1]) : A;
  const C = P[2] ? xy(P[2]) : B;
  const D = P[3] ? xy(P[3]) : C;
  const levels = s.levels ?? FIB_LEVELS;

  switch (d.tool) {
    // --- Linien --------------------------------------------------------------------
    case "trend":
    case "ray":
    case "extended":
      seg(A, B);
      break;
    case "infoline": {
      seg(A, B);
      const a = P[0]!;
      const b = P[1] ?? a;
      const bars = Math.round(Math.abs(b.t - a.t) / ctx.interval);
      text((A[0] + B[0]) / 2 + 8, (A[1] + B[1]) / 2, `${ctx.fmtP(b.p - a.p)} (${pct(a.p, b.p)}) · ${bars} Spalten · ${dur(b.t - a.t)}`, {
        bg: "rgba(26,27,30,0.85)",
      });
      break;
    }
    case "trendangle": {
      seg(A, B);
      const deg = (Math.atan2(A[1] - B[1], B[0] - A[0]) * 180) / Math.PI;
      line([A, [A[0] + 60, A[1]]], { dash: "dotted", alpha: 0.6 });
      out.push(...arcLine(A, 30, 0, -deg, s.color));
      text(A[0] + 36, A[1] - 10, `${deg.toFixed(1)}°`);
      break;
    }
    case "hline":
      line([[-BIG, A[1]], [BIG, A[1]]]);
      text(ctx.pw - 4, A[1] - 9, ctx.fmtP(P[0]!.p) + (s.text ? `  ${s.text}` : ""), { align: "right" });
      break;
    case "hray":
      line([A, [BIG, A[1]]]);
      text(A[0] + 4, A[1] - 9, ctx.fmtP(P[0]!.p) + (s.text ? `  ${s.text}` : ""));
      break;
    case "vline":
      line([[A[0], -BIG], [A[0], BIG]]);
      text(A[0] + 4, ctx.ph - 10, ctx.fmtT(P[0]!.t), { noHit: true });
      break;
    case "crossline":
      line([[-BIG, A[1]], [BIG, A[1]]]);
      line([[A[0], -BIG], [A[0], BIG]]);
      break;
    // --- Kanäle --------------------------------------------------------------------
    case "parallel": {
      if (P.length < 3) {
        seg(A, B);
        break;
      }
      const off = C[1] - lineY(A, B, C[0]);
      const A2: XY = [A[0], A[1] + off];
      const B2: XY = [B[0], B[1] + off];
      poly([A, B, B2, A2], s.fill, { color: null });
      seg(A, B);
      seg(A2, B2);
      seg([A[0], A[1] + off / 2], [B[0], B[1] + off / 2], s.extendLeft, s.extendRight, { dash: "dashed", alpha: 0.6 });
      break;
    }
    case "regression": {
      const a = P[0]!;
      const b = P[1] ?? a;
      const r = regression(ctx.bars, a.t, b.t);
      if (!r) {
        seg(A, B, false, false, { dash: "dashed" });
        text(B[0] + 4, B[1], "zu wenig Kerzen im Bereich", { noHit: true });
        break;
      }
      const at = (t: number, k: number) => ctx.toY(r.a + r.b * t + k * r.sd);
      const x0 = ctx.toX(Math.min(a.t, b.t));
      const x1 = ctx.toX(Math.max(a.t, b.t));
      const lo = Math.min(a.t, b.t);
      const hi = Math.max(a.t, b.t);
      poly([[x0, at(lo, 2)], [x1, at(hi, 2)], [x1, at(hi, -2)], [x0, at(lo, -2)]], s.fill, { color: null });
      line([[x0, at(lo, 0)], [x1, at(hi, 0)]], { dash: "dashed" });
      line([[x0, at(lo, 2)], [x1, at(hi, 2)]]);
      line([[x0, at(lo, -2)], [x1, at(hi, -2)]]);
      text(x1 + 4, at(hi, 2), `+2σ (${r.n} Kerzen)`, { noHit: true, size: 10 });
      break;
    }
    case "flattop": {
      seg(A, B);
      if (P.length < 3) break;
      const F1: XY = [A[0], C[1]];
      const F2: XY = [B[0], C[1]];
      poly([A, B, F2, F1], s.fill, { color: null });
      seg(F1, F2);
      break;
    }
    case "disjoint":
      seg(A, B);
      if (P.length >= 3) seg(C, D);
      if (P.length >= 4) poly([A, B, D, C], s.fill, { color: null });
      break;
    // --- Pitchforks ----------------------------------------------------------------
    case "pitchfork":
    case "schiff":
    case "modschiff":
    case "insidefork": {
      if (P.length < 3) {
        line([A, B]);
        break;
      }
      const M: XY = [(B[0] + C[0]) / 2, (B[1] + C[1]) / 2];
      let O = A;
      if (d.tool === "schiff") O = [A[0], (A[1] + B[1]) / 2];
      if (d.tool === "modschiff") O = [(A[0] + B[0]) / 2, (A[1] + B[1]) / 2];
      line([A, B], { alpha: 0.6 });
      line([B, C]);
      const dir: XY = [M[0] - O[0], M[1] - O[1]];
      const ray = (from: XY): XY[] => extendSeg(from, [from[0] + dir[0], from[1] + dir[1]], false, true);
      line(ray(O));
      line(ray(B));
      line(ray(C));
      if (d.tool === "insidefork") {
        for (const f of [0.5]) {
          line(ray([B[0] + (M[0] - B[0]) * f, B[1] + (M[1] - B[1]) * f]), { dash: "dashed", alpha: 0.6 });
          line(ray([C[0] + (M[0] - C[0]) * f, C[1] + (M[1] - C[1]) * f]), { dash: "dashed", alpha: 0.6 });
        }
      }
      break;
    }
    // --- Fibonacci -----------------------------------------------------------------
    case "fibret": {
      const a = P[0]!;
      const b = P[1] ?? a;
      const x0 = Math.min(A[0], B[0]);
      const x1 = s.extendRight ? BIG : Math.max(A[0], B[0]);
      levels.forEach((l, i) => {
        const p = b.p + (a.p - b.p) * l;
        const y = ctx.toY(p);
        const col = LEVEL_COLORS[i % LEVEL_COLORS.length]!;
        const next = levels[i + 1];
        if (next !== undefined) {
          const y2 = ctx.toY(b.p + (a.p - b.p) * next);
          poly([[x0, y], [x1, y], [x1, y2], [x0, y2]], col, { color: null, alpha: 0.08 });
        }
        line([[x0, y], [x1, y]], { color: col, dash: "solid" });
        text(x0 - 4, y, `${l} (${ctx.fmtP(p)})`, { color: col, align: "right", size: 10 });
      });
      seg(A, B, false, false, { dash: "dashed", alpha: 0.5 });
      break;
    }
    case "fibext": {
      line([A, B, C], { dash: "dashed", alpha: 0.6 });
      if (P.length < 3) break;
      const [a, b, c] = [P[0]!, P[1]!, P[2]!];
      const x0 = C[0];
      const x1 = s.extendRight ? BIG : Math.max(C[0] + 160, B[0]);
      levels.forEach((l, i) => {
        const p = c.p + (b.p - a.p) * l;
        const y = ctx.toY(p);
        const col = LEVEL_COLORS[i % LEVEL_COLORS.length]!;
        line([[x0, y], [x1, y]], { color: col, dash: "solid" });
        text(x1 + 4, y, `${l} (${ctx.fmtP(p)})`, { color: col, size: 10 });
      });
      break;
    }
    case "fibchannel": {
      seg(A, B);
      if (P.length < 3) break;
      const off = C[1] - lineY(A, B, C[0]);
      levels.forEach((l, i) => {
        const col = LEVEL_COLORS[i % LEVEL_COLORS.length]!;
        seg([A[0], A[1] + off * l], [B[0], B[1] + off * l], s.extendLeft, true, { color: col, dash: "solid" });
        text(A[0] - 4, A[1] + off * l, String(l), { color: col, align: "right", size: 10 });
      });
      break;
    }
    case "fibtimezone": {
      const a = P[0]!;
      const b = P[1] ?? a;
      const span = b.t - a.t;
      const fibs = [0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89];
      for (const [i, f] of fibs.entries()) {
        const x = ctx.toX(a.t + span * f);
        line([[x, -BIG], [x, BIG]], { color: LEVEL_COLORS[i % LEVEL_COLORS.length], dash: "solid" });
        text(x + 3, 12, String(f), { size: 10, noHit: true });
        if (!span) break;
      }
      break;
    }
    case "fibfan":
    case "gannfan": {
      const dx = B[0] - A[0];
      const dy = B[1] - A[1];
      const ratios =
        d.tool === "gannfan"
          ? [[1, 8], [1, 4], [1, 3], [1, 2], [1, 1], [2, 1], [3, 1], [4, 1], [8, 1]].map(([t, p]) => ({ k: p! / t!, l: `${t}x${p}` }))
          : [...levels.map((l) => ({ k: l, l: String(l) })), { k: 1, l: "1" }];
      ratios.forEach(({ k, l }, i) => {
        const end: XY = [A[0] + dx, A[1] + dy * k];
        const col = LEVEL_COLORS[(i + 1) % LEVEL_COLORS.length]!;
        line(extendSeg(A, end, false, true), { color: col, dash: "solid" });
        text(end[0] + 4, end[1], l, { color: col, size: 10 });
      });
      if (d.tool === "fibfan") line([A, [B[0], A[1]], B, [A[0], B[1]], A], { dash: "dotted", alpha: 0.4 });
      break;
    }
    case "fibtime": {
      line([A, B, C], { dash: "dashed", alpha: 0.6 });
      if (P.length < 3) break;
      const span = P[1]!.t - P[0]!.t;
      levels.forEach((l, i) => {
        const x = ctx.toX(P[2]!.t + span * l);
        line([[x, -BIG], [x, BIG]], { color: LEVEL_COLORS[i % LEVEL_COLORS.length], dash: "solid" });
        text(x + 3, 12, String(l), { size: 10, noHit: true });
      });
      break;
    }
    case "fibcircles": {
      const r = Math.hypot(B[0] - A[0], B[1] - A[1]);
      const cx = (A[0] + B[0]) / 2;
      const cy = (A[1] + B[1]) / 2;
      line([A, B], { dash: "dashed", alpha: 0.5 });
      levels.forEach((l, i) => line(arcPts(cx, cy, (r / 2) * l * 2, (r / 2) * l * 2), { color: LEVEL_COLORS[(i + 1) % LEVEL_COLORS.length], dash: "solid" }));
      break;
    }
    case "fibspiral": {
      // Goldene Spirale: Radius wächst je Vierteldrehung um phi, geht genau durch B
      const r0 = Math.hypot(B[0] - A[0], B[1] - A[1]);
      const a0 = Math.atan2(B[1] - A[1], B[0] - A[0]);
      const phi = (1 + Math.sqrt(5)) / 2;
      const pts: XY[] = [];
      for (let i = 0; i <= 400; i++) {
        const th = -Math.PI * 6 + (i / 400) * Math.PI * 7;
        const r = r0 * Math.pow(phi, th / (Math.PI / 2));
        pts.push([A[0] + Math.cos(a0 + th) * r, A[1] + Math.sin(a0 + th) * r]);
      }
      line(pts);
      line([A, B], { dash: "dashed", alpha: 0.5 });
      break;
    }
    case "fibarcs": {
      const r = Math.hypot(B[0] - A[0], B[1] - A[1]);
      const up = B[1] < A[1];
      line([A, B], { dash: "dashed", alpha: 0.5 });
      levels.forEach((l, i) =>
        line(arcPts(B[0], B[1], r * l, r * l, up ? 0 : Math.PI, up ? Math.PI : Math.PI * 2), {
          color: LEVEL_COLORS[(i + 1) % LEVEL_COLORS.length],
          dash: "solid",
        }),
      );
      break;
    }
    case "fibwedge": {
      line([A, B]);
      if (P.length < 3) break;
      line([A, C]);
      const r = Math.hypot(B[0] - A[0], B[1] - A[1]);
      const a0 = Math.atan2(B[1] - A[1], B[0] - A[0]);
      let a1 = Math.atan2(C[1] - A[1], C[0] - A[0]);
      if (a1 - a0 > Math.PI) a1 -= Math.PI * 2;
      if (a0 - a1 > Math.PI) a1 += Math.PI * 2;
      levels.forEach((l, i) => line(arcPts(A[0], A[1], r * l, r * l, a0, a1, 32), { color: LEVEL_COLORS[(i + 1) % LEVEL_COLORS.length], dash: "solid" }));
      break;
    }
    case "pitchfan": {
      line([A, B]);
      if (P.length < 3) break;
      line([B, C]);
      const M: XY = [(B[0] + C[0]) / 2, (B[1] + C[1]) / 2];
      line(extendSeg(A, M, false, true));
      levels.forEach((l, i) => {
        const col = LEVEL_COLORS[(i + 1) % LEVEL_COLORS.length];
        line(extendSeg(A, [M[0] + (B[0] - M[0]) * l, M[1] + (B[1] - M[1]) * l], false, true), { color: col, dash: "solid" });
        line(extendSeg(A, [M[0] + (C[0] - M[0]) * l, M[1] + (C[1] - M[1]) * l], false, true), { color: col, dash: "solid" });
      });
      break;
    }
    // --- Gann ----------------------------------------------------------------------
    case "gannbox": {
      const lv = s.levels ?? [0, 0.25, 0.382, 0.5, 0.618, 0.75, 1];
      poly([A, [B[0], A[1]], B, [A[0], B[1]]], null);
      for (const l of lv) {
        const x = A[0] + (B[0] - A[0]) * l;
        const y = A[1] + (B[1] - A[1]) * l;
        line([[x, A[1]], [x, B[1]]], { dash: "dotted", alpha: 0.6 });
        line([[A[0], y], [B[0], y]], { dash: "dotted", alpha: 0.6 });
      }
      line([A, B], { alpha: 0.5 });
      line([[A[0], B[1]], [B[0], A[1]]], { alpha: 0.5 });
      break;
    }
    case "gannsq":
    case "gannsqfixed": {
      // Quadrat in Pixeln (Gann denkt in "1 Zeiteinheit = 1 Preiseinheit")
      const Q: XY = P.length >= 2 ? B : [A[0] + 160, A[1] - 160];
      const side = Math.max(Math.abs(Q[0] - A[0]), Math.abs(Q[1] - A[1]));
      const sx = Q[0] >= A[0] ? 1 : -1;
      const sy = Q[1] >= A[1] ? 1 : -1;
      const E: XY = [A[0] + side * sx, A[1] + side * sy];
      poly([A, [E[0], A[1]], E, [A[0], E[1]]], null);
      for (const f of [0.25, 0.5, 0.75]) {
        line([[A[0] + (E[0] - A[0]) * f, A[1]], [A[0] + (E[0] - A[0]) * f, E[1]]], { dash: "dotted", alpha: 0.5 });
        line([[A[0], A[1] + (E[1] - A[1]) * f], [E[0], A[1] + (E[1] - A[1]) * f]], { dash: "dotted", alpha: 0.5 });
      }
      line([A, E]);
      line([[A[0], E[1]], [E[0], A[1]]]);
      line(arcPts(A[0], A[1], side, side, sx > 0 ? (sy > 0 ? 0 : -Math.PI / 2) : sy > 0 ? Math.PI / 2 : Math.PI, sx > 0 ? (sy > 0 ? Math.PI / 2 : 0) : sy > 0 ? Math.PI : (Math.PI * 3) / 2, 32), { alpha: 0.6 });
      break;
    }
    // --- Muster --------------------------------------------------------------------
    case "xabcd":
    case "cypher":
    case "abcd":
    case "headshoulders":
    case "trianglepattern":
    case "threedrives":
    case "elliottimpulse":
    case "elliottcorrection":
    case "elliotttriangle":
    case "elliottdouble":
    case "elliotttriple": {
      const pts = P.map(xy);
      if ((d.tool === "xabcd" || d.tool === "cypher") && pts.length >= 3) {
        poly([pts[0]!, pts[1]!, pts[2]!], s.fill, { color: null });
        if (pts.length >= 5) poly([pts[2]!, pts[3]!, pts[4]!], s.fill, { color: null });
      }
      if (d.tool === "headshoulders" && pts.length >= 7) {
        poly(pts.slice(1, 6), s.fill, { color: null });
        line(extendSeg(pts[2]!, pts[4]!, true, true), { dash: "dashed" });
        text(pts[4]![0] + 6, pts[4]![1] + 12, "Nackenlinie", { size: 10 });
      }
      if (d.tool === "trianglepattern" && pts.length >= 4) {
        line(extendSeg(pts[0]!, pts[2]!, false, true), { dash: "dashed" });
        line(extendSeg(pts[1]!, pts[3]!, false, true), { dash: "dashed" });
      }
      line(pts);
      const labels = PATTERN_LABELS[d.tool] ?? [];
      pts.forEach((p, i) => {
        const prev = P[i - 1];
        const isHigh = prev ? P[i]!.p >= prev.p : P[1] ? P[0]!.p >= P[1].p : true;
        const lab = labels[i];
        if (lab) text(p[0], p[1] + (isHigh ? -14 : 14), lab, { align: "center", bg: "rgba(26,27,30,0.8)" });
      });
      // Fibonacci-Verhältnisse der Schenkel (XABCD/Cypher/ABCD)
      if ((d.tool === "xabcd" || d.tool === "cypher") && P.length >= 5) {
        const [X, Aa, Bb, Cc, Dd] = P as [Pt, Pt, Pt, Pt, Pt];
        const mid = (u: XY, v: XY): [number, number] => [(u[0] + v[0]) / 2, (u[1] + v[1]) / 2];
        line([pts[0]!, pts[2]!], { dash: "dashed", alpha: 0.5 });
        line([pts[2]!, pts[4]!], { dash: "dashed", alpha: 0.5 });
        const m1 = mid(pts[0]!, pts[2]!);
        const m2 = mid(pts[2]!, pts[4]!);
        text(m1[0], m1[1], ratio(X, Aa, Aa, Bb), { align: "center", size: 10, bg: "rgba(26,27,30,0.8)" });
        text(m2[0], m2[1], ratio(Bb, Cc, Cc, Dd), { align: "center", size: 10, bg: "rgba(26,27,30,0.8)" });
        const xd = Math.abs(Dd.p - Aa.p) / (Math.abs(Aa.p - X.p) || 1);
        text(pts[4]![0] + 8, pts[4]![1], `XD ${xd.toFixed(3)}`, { size: 10 });
      }
      if (d.tool === "abcd" && P.length >= 4) {
        text(pts[3]![0] + 8, pts[3]![1], `CD/AB ${(Math.abs(P[3]!.p - P[2]!.p) / (Math.abs(P[1]!.p - P[0]!.p) || 1)).toFixed(3)}`, { size: 10 });
      }
      break;
    }
    // --- Zyklen --------------------------------------------------------------------
    case "cycliclines":
    case "timecycles": {
      const a = P[0]!;
      const b = P[1] ?? a;
      const span = Math.abs(b.t - a.t);
      if (!span) break;
      const t0 = Math.min(a.t, b.t);
      const tMin = ctx.toT(0) - span;
      const tMax = ctx.toT(ctx.pw) + span;
      let k = Math.floor((tMin - t0) / span);
      for (let t = t0 + k * span; t <= tMax && k < 400; t += span, k++) {
        const x = ctx.toX(t);
        if (d.tool === "cycliclines") line([[x, -BIG], [x, BIG]], { alpha: t === t0 ? 1 : 0.6 });
        else {
          const w = ctx.toX(t + span) - x;
          line(arcPts(x + w / 2, A[1], w / 2, w / 2, Math.PI, Math.PI * 2, 24));
        }
      }
      break;
    }
    case "sine": {
      const a = P[0]!;
      const b = P[1] ?? a;
      const half = Math.abs(b.t - a.t) || ctx.interval;
      const amp = (b.p - a.p) / 2;
      const mid = (a.p + b.p) / 2;
      const pts: XY[] = [];
      for (let x = 0; x <= ctx.pw; x += 3) {
        const t = ctx.toT(x);
        pts.push([x, ctx.toY(mid - amp * Math.cos((Math.PI * (t - a.t)) / half))]);
      }
      line(pts);
      break;
    }
    // --- Prognose ------------------------------------------------------------------
    case "long":
    case "short": {
      // pts: [0] Einstieg (Zeit, Preis), [1] Ende/Ziel (Zeit, Zielpreis), [2] Stop (Zeit, Stoppreis)
      const e = P[0]!;
      const long = d.tool === "long";
      const tEnd = P[1]?.t ?? e.t + ctx.interval * 60;
      const tp = P[1]?.p ?? e.p * (long ? 1.01 : 0.99);
      const sl = P[2]?.p ?? e.p * (long ? 0.995 : 1.005);
      const x0 = A[0];
      const x1 = ctx.toX(tEnd);
      const ye = A[1];
      poly([[x0, ye], [x1, ye], [x1, ctx.toY(tp)], [x0, ctx.toY(tp)]], "rgba(85,229,168,0.18)", { color: null });
      poly([[x0, ye], [x1, ye], [x1, ctx.toY(sl)], [x0, ctx.toY(sl)]], "rgba(255,115,133,0.18)", { color: null });
      line([[x0, ye], [x1, ye]], { color: "#c9ccd1", dash: "solid", width: 1 });
      const rr = Math.abs(tp - e.p) / (Math.abs(e.p - sl) || 1);
      const lab = (y: number, t: string, col: string) => text((x0 + x1) / 2, y, t, { align: "center", color: "#16171a", bg: col, size: 11 });
      lab(ctx.toY(tp) + (long ? -10 : 10), `Ziel ${ctx.fmtP(tp)} (${pct(e.p, tp)})`, "#55e5a8");
      lab(ctx.toY(sl) + (long ? 10 : -10), `Stop ${ctx.fmtP(sl)} (${pct(e.p, sl)})`, "#ff7385");
      lab(ye, `${long ? "Long" : "Short"} ${ctx.fmtP(e.p)} · CRV ${rr.toFixed(2)}`, "#c9ccd1");
      break;
    }
    case "forecast": {
      const a = P[0]!;
      const b = P[1] ?? a;
      out.push(...arrowPrims(A, B, s.color, s.width, 10));
      text(B[0] + 6, B[1], `${pct(a.p, b.p)} · ${dur(b.t - a.t)}`, { bg: "rgba(26,27,30,0.85)" });
      break;
    }
    case "barspattern": {
      line([A, B], { dash: "dotted", alpha: 0.5 });
      if (P.length < 3) break;
      const [a, b, c] = [P[0]!, P[1]!, P[2]!];
      const src = ctx.bars.filter((x) => x.t >= Math.min(a.t, b.t) && x.t <= Math.max(a.t, b.t));
      if (!src.length) break;
      const dt = c.t - src[0]!.t;
      const dp = c.p - src[0]!.o;
      for (const k of src) {
        const x = ctx.toX(k.t + dt + 30_000);
        const w = Math.max(1, (ctx.toX(k.t + dt + 60_000) - ctx.toX(k.t + dt)) * 0.35);
        const up = k.c >= k.o;
        line([[x, ctx.toY(k.h + dp)], [x, ctx.toY(k.l + dp)]], { color: up ? "#55e5a8" : "#ff7385", alpha: 0.5, dash: "solid", width: 1 });
        poly([[x - w, ctx.toY(k.o + dp)], [x + w, ctx.toY(k.o + dp)], [x + w, ctx.toY(k.c + dp)], [x - w, ctx.toY(k.c + dp)]], up ? "#55e5a8" : "#ff7385", { color: null, alpha: 0.35 });
      }
      break;
    }
    case "ghostfeed": {
      const pts = P.map(xy);
      for (let i = 0; i + 1 < pts.length; i++) {
        const a = pts[i]!;
        const b = pts[i + 1]!;
        const n = 6;
        for (let k = 0; k < n; k++) {
          const f0 = k / n;
          const f1 = (k + 1) / n;
          const o: XY = [a[0] + (b[0] - a[0]) * f0, a[1] + (b[1] - a[1]) * f0];
          const cl: XY = [a[0] + (b[0] - a[0]) * f1, a[1] + (b[1] - a[1]) * f1];
          const w = Math.max(2, Math.abs(b[0] - a[0]) / n / 3);
          const x = (o[0] + cl[0]) / 2;
          const up = cl[1] < o[1];
          line([[x, Math.min(o[1], cl[1]) - 4], [x, Math.max(o[1], cl[1]) + 4]], { color: "#c9ccd1", alpha: 0.5, width: 1, dash: "solid" });
          poly([[x - w, o[1]], [x + w, o[1]], [x + w, cl[1]], [x - w, cl[1]]], up ? "#55e5a8" : "#ff7385", { color: null, alpha: 0.35 });
        }
      }
      if (pts.length === 1) line([pts[0]!, pts[0]!]);
      break;
    }
    case "sector": {
      line([A, B]);
      if (P.length < 3) break;
      const r = Math.hypot(B[0] - A[0], B[1] - A[1]);
      const a0 = Math.atan2(B[1] - A[1], B[0] - A[0]);
      let a1 = Math.atan2(C[1] - A[1], C[0] - A[0]);
      if (a1 - a0 > Math.PI) a1 -= Math.PI * 2;
      if (a0 - a1 > Math.PI) a1 += Math.PI * 2;
      const arc = arcPts(A[0], A[1], r, r, a0, a1, 32);
      poly([A, ...arc], s.fill);
      text(A[0] + 8, A[1], `${(((a1 - a0) * 180) / Math.PI).toFixed(1)}°`, { size: 10 });
      break;
    }
    case "avwap": {
      const v = anchoredVwap(ctx.bars, P[0]!.t);
      const pts: XY[] = [A, ...v.map((q) => [ctx.toX(q.t), ctx.toY(q.p)] as XY)];
      line(pts);
      const last = v[v.length - 1];
      if (last) text(ctx.toX(last.t) + 4, ctx.toY(last.p), `VWAP ${ctx.fmtP(last.p)}`, { size: 10 });
      else text(A[0] + 4, A[1], "VWAP: keine Kerzen ab hier", { size: 10 });
      break;
    }
    case "fixedvp":
    case "anchoredvp": {
      const t0 = P[0]!.t;
      const t1 = d.tool === "anchoredvp" ? ctx.toT(ctx.pw) : (P[1] ?? P[0]!).t;
      const vp = volumeProfile(ctx, t0, t1);
      const x0 = ctx.toX(Math.min(t0, t1));
      const x1 = ctx.toX(Math.max(t0, t1));
      line([[x0, -BIG], [x0, BIG]], { dash: "dotted", alpha: 0.4 });
      if (d.tool === "fixedvp") line([[x1, -BIG], [x1, BIG]], { dash: "dotted", alpha: 0.4 });
      if (!vp) break;
      const max = Math.max(...vp.vol) || 1;
      const width = Math.max(40, (x1 - x0) * 0.4);
      vp.vol.forEach((v, r) => {
        const y0 = ctx.toY(vp.lo + vp.step * r);
        const y1 = ctx.toY(vp.lo + vp.step * (r + 1));
        const inVa = r >= vp.vaLo && r <= vp.vaHi;
        poly([[x0, y0], [x0 + (v / max) * width, y0], [x0 + (v / max) * width, y1 + 1], [x0, y1 + 1]], r === vp.poc ? "#ffa833" : inVa ? "rgba(90,204,255,0.55)" : "rgba(90,204,255,0.25)", { color: null });
      });
      const yPoc = ctx.toY(vp.lo + vp.step * (vp.poc + 0.5));
      line([[x0, yPoc], [x1, yPoc]], { color: "#ffa833", dash: "dashed", width: 1 });
      text(x0 + 4, ctx.toY(vp.lo + vp.step * (vp.vaHi + 1)) - 8, `POC ${ctx.fmtP(vp.lo + vp.step * (vp.poc + 0.5))}${vp.exact ? "" : " · aus 1m-Kerzen geschätzt"}`, { size: 10 });
      break;
    }
    // --- Messen --------------------------------------------------------------------
    case "pricerange":
    case "daterange":
    case "dateprice": {
      const a = P[0]!;
      const b = P[1] ?? a;
      const up = b.p >= a.p;
      const fill = d.tool === "daterange" ? "rgba(90,204,255,0.15)" : up ? "rgba(90,204,255,0.15)" : "rgba(255,115,133,0.15)";
      poly([A, [B[0], A[1]], B, [A[0], B[1]]], fill, { color: null });
      const midX = (A[0] + B[0]) / 2;
      const midY = (A[1] + B[1]) / 2;
      if (d.tool !== "daterange") out.push(...arrowPrims([midX, A[1]], [midX, B[1]], s.color, 1, 7));
      if (d.tool !== "pricerange") out.push(...arrowPrims([A[0], midY], [B[0], midY], s.color, 1, 7));
      const parts: string[] = [];
      if (d.tool !== "daterange") parts.push(`${ctx.fmtP(b.p - a.p)} (${pct(a.p, b.p)}) · ${Math.round(Math.abs(b.p - a.p) / ctx.tick)} Ticks`);
      if (d.tool !== "pricerange") {
        const vol = ctx.bars.filter((k) => k.t >= Math.min(a.t, b.t) && k.t < Math.max(a.t, b.t)).reduce((sum, k) => sum + k.v, 0);
        parts.push(`${Math.round(Math.abs(b.t - a.t) / ctx.interval)} Spalten · ${dur(b.t - a.t)}${vol ? ` · Vol ${vol.toLocaleString("de-DE", { maximumFractionDigits: 2 })}` : ""}`);
      }
      text(midX, Math.max(A[1], B[1]) + 14, parts.join("\n"), { align: "center", bg: "rgba(26,27,30,0.9)", color: "#dfe1e4" });
      break;
    }
    // --- Formen --------------------------------------------------------------------
    case "rect":
      poly([A, [B[0], A[1]], B, [A[0], B[1]]], s.fill);
      if (s.text) text((A[0] + B[0]) / 2, (A[1] + B[1]) / 2, s.text, { align: "center" });
      break;
    case "rotrect": {
      if (P.length < 3) {
        line([A, B]);
        break;
      }
      const dx = B[0] - A[0];
      const dy = B[1] - A[1];
      const len = Math.hypot(dx, dy) || 1;
      const nx = -dy / len;
      const ny = dx / len;
      const h = (C[0] - B[0]) * nx + (C[1] - B[1]) * ny;
      poly([A, B, [B[0] + nx * h, B[1] + ny * h], [A[0] + nx * h, A[1] + ny * h]], s.fill);
      break;
    }
    case "path":
      if (P.length >= 2) {
        const pts = P.map(xy);
        line(pts);
        out.push(...arrowPrims(pts[pts.length - 2]!, pts[pts.length - 1]!, s.color, s.width, 10, true));
      } else line([A, A]);
      break;
    case "polyline":
      if (P.length >= 3) poly(P.map(xy), s.fill);
      else line(P.map(xy));
      break;
    case "circle": {
      const r = Math.hypot(B[0] - A[0], B[1] - A[1]);
      poly(arcPts(A[0], A[1], r, r), s.fill);
      break;
    }
    case "ellipse":
      poly(arcPts((A[0] + B[0]) / 2, (A[1] + B[1]) / 2, Math.abs(B[0] - A[0]) / 2, Math.abs(B[1] - A[1]) / 2), s.fill);
      break;
    case "triangle":
      if (P.length >= 3) poly([A, B, C], s.fill);
      else line([A, B]);
      break;
    case "arc": {
      if (P.length < 3) {
        line([A, B]);
        break;
      }
      const c = circleThrough(A, B, C);
      if (!c) {
        line([A, C]);
        break;
      }
      let a0 = Math.atan2(A[1] - c.cy, A[0] - c.cx);
      let a1 = Math.atan2(C[1] - c.cy, C[0] - c.cx);
      const am = Math.atan2(B[1] - c.cy, B[0] - c.cx);
      // Richtung so wählen, dass der Bogen durch B läuft
      const norm = (x: number) => ((x % (Math.PI * 2)) + Math.PI * 2) % (Math.PI * 2);
      if (norm(am - a0) > norm(a1 - a0)) [a0, a1] = [a1, a0];
      if (a1 < a0) a1 += Math.PI * 2;
      line(arcPts(c.cx, c.cy, c.r, c.r, a0, a1, 64));
      break;
    }
    case "curve":
      line(P.length >= 3 ? quadPts(A, B, C) : [A, B]);
      break;
    case "doublecurve":
      line(P.length >= 4 ? cubicPts(A, B, C, D) : P.length === 3 ? quadPts(A, B, C) : [A, B]);
      break;
    case "brush":
    case "highlighter":
      line(P.map(xy), { dash: "solid" });
      break;
    case "arrow":
      out.push(...arrowPrims(A, B, s.color, s.width, 12));
      break;
    case "arrowmarker": {
      const dx = B[0] - A[0];
      const dy = B[1] - A[1];
      const len = Math.hypot(dx, dy) || 1;
      const ux = dx / len;
      const uy = dy / len;
      const w = Math.max(6, len * 0.12);
      const head = Math.min(len * 0.45, w * 2.5);
      const nx = -uy;
      const ny = ux;
      const H: XY = [B[0] - ux * head, B[1] - uy * head];
      poly(
        [
          [A[0] + nx * w * 0.5, A[1] + ny * w * 0.5],
          [H[0] + nx * w * 0.5, H[1] + ny * w * 0.5],
          [H[0] + nx * w * 1.3, H[1] + ny * w * 1.3],
          B,
          [H[0] - nx * w * 1.3, H[1] - ny * w * 1.3],
          [H[0] - nx * w * 0.5, H[1] - ny * w * 0.5],
          [A[0] - nx * w * 0.5, A[1] - ny * w * 0.5],
        ],
        s.fill,
        { color: null },
      );
      break;
    }
    case "arrowup":
    case "arrowdown":
    case "arrowleft":
    case "arrowright": {
      const dir: Record<string, XY> = { arrowup: [0, -1], arrowdown: [0, 1], arrowleft: [-1, 0], arrowright: [1, 0] };
      const [ux, uy] = dir[d.tool]!;
      const tip: XY = [A[0], A[1]];
      const base: XY = [A[0] - ux * 22, A[1] - uy * 22];
      const nx = -uy;
      const ny = ux;
      poly([tip, [base[0] + nx * 9 + ux * 10, base[1] + ny * 9 + uy * 10], [base[0] + nx * 4 + ux * 10, base[1] + ny * 4 + uy * 10], [base[0] + nx * 4, base[1] + ny * 4], [base[0] - nx * 4, base[1] - ny * 4], [base[0] - nx * 4 + ux * 10, base[1] - ny * 4 + uy * 10], [base[0] - nx * 9 + ux * 10, base[1] - ny * 9 + uy * 10]], s.color, { color: null });
      if (s.text) text(base[0] - ux * 6, base[1] - uy * 6 + (uy === 0 ? 0 : uy < 0 ? 8 : -8), s.text, { align: "center", size: 11 });
      break;
    }
    // --- Text & Notizen ------------------------------------------------------------
    case "text":
      text(A[0], A[1], s.text || "Text", { base: "middle" });
      break;
    case "icon":
      text(A[0], A[1], s.text || "⭐", { align: "center", base: "middle" });
      break;
    case "note":
    case "comment":
    case "pin": {
      const msg = s.text || (d.tool === "pin" ? "Pin" : "Notiz");
      if (d.tool === "pin") {
        poly(arcPts(A[0], A[1] - 14, 6, 6), s.color, { color: null });
        line([A, [A[0], A[1] - 8]], { width: 2, dash: "solid" });
        text(A[0] + 10, A[1] - 14, msg, { bg: "rgba(26,27,30,0.9)", color: "#dfe1e4" });
      } else {
        poly([A, [A[0] + 8, A[1] - 10], [A[0] - 2, A[1] - 10]], d.tool === "comment" ? "rgba(26,27,30,0.92)" : s.color, { color: s.color });
        text(A[0] - 6, A[1] - 22, msg, { bg: d.tool === "comment" ? "rgba(26,27,30,0.92)" : s.color, color: d.tool === "comment" ? "#dfe1e4" : "#16171a" });
      }
      break;
    }
    case "pricenote":
      line([A, B], { dash: "solid" });
      poly(arcPts(A[0], A[1], 3, 3), s.color, { color: null });
      text(B[0] + 4, B[1], `${ctx.fmtP(P[0]!.p)}${s.text ? `  ${s.text}` : ""}`, { bg: s.color, color: "#16171a" });
      break;
    case "callout":
      line([A, B], { dash: "solid" });
      text(B[0], B[1], s.text || "Hinweis", { bg: "rgba(26,27,30,0.92)", color: "#dfe1e4", align: "center" });
      break;
    case "pricelabel":
      poly([A, [A[0] + 8, A[1] - 9], [A[0] + 8 + 70, A[1] - 9], [A[0] + 8 + 70, A[1] + 9], [A[0] + 8, A[1] + 9]], s.color, { color: null });
      text(A[0] + 12, A[1], ctx.fmtP(P[0]!.p), { color: "#16171a", noHit: true });
      break;
    case "signpost":
      line([A, [A[0], A[1] - 40]], { dash: "solid" });
      text(A[0], A[1] - 50, s.text || "📌", { align: "center", bg: "rgba(26,27,30,0.9)", color: "#dfe1e4" });
      break;
    case "table": {
      const rows = (s.text || "Spalte A|Spalte B\n1|2").split("\n").map((r) => r.split("|"));
      const cols = Math.max(...rows.map((r) => r.length));
      const cw = 90;
      const rh = 20;
      poly([A, [A[0] + cols * cw, A[1]], [A[0] + cols * cw, A[1] + rows.length * rh], [A[0], A[1] + rows.length * rh]], "rgba(26,27,30,0.92)");
      rows.forEach((r, i) => {
        if (i) line([[A[0], A[1] + i * rh], [A[0] + cols * cw, A[1] + i * rh]], { width: 1, alpha: 0.5, dash: "solid" });
        r.forEach((cell, j) => text(A[0] + j * cw + 6, A[1] + i * rh + rh / 2, cell.trim(), { color: "#dfe1e4", noHit: true, size: 11 }));
      });
      for (let j = 1; j < cols; j++) line([[A[0] + j * cw, A[1]], [A[0] + j * cw, A[1] + rows.length * rh]], { width: 1, alpha: 0.5, dash: "solid" });
      break;
    }
  }
  return out;
}

/** Preis-y der Geraden a-b an der Stelle x. */
function lineY(a: XY, b: XY, x: number): number {
  const dx = b[0] - a[0];
  return dx ? a[1] + ((b[1] - a[1]) * (x - a[0])) / dx : a[1];
}

function arcLine(c: XY, r: number, deg0: number, deg1: number, color: string): Prim[] {
  const a0 = (-deg0 * Math.PI) / 180;
  const a1 = (deg1 * Math.PI) / 180;
  return [{ kind: "line", pts: arcPts(c[0], c[1], r, r, Math.min(a0, a1), Math.max(a0, a1), 20), color, width: 1, dash: "solid", alpha: 0.6 }];
}

export function arrowPrims(a: XY, b: XY, color: string, width: number, head: number, headOnly = false): Prim[] {
  const ang = Math.atan2(b[1] - a[1], b[0] - a[0]);
  const h1: XY = [b[0] - Math.cos(ang - 0.45) * head, b[1] - Math.sin(ang - 0.45) * head];
  const h2: XY = [b[0] - Math.cos(ang + 0.45) * head, b[1] - Math.sin(ang + 0.45) * head];
  const out: Prim[] = [];
  if (!headOnly) out.push({ kind: "line", pts: [a, b], color, width, dash: "solid" });
  out.push({ kind: "line", pts: [h1, b, h2], color, width, dash: "solid" });
  return out;
}

/** Ungefähre Textbreite (für Klickflächen, ohne Canvas). */
export function textWidth(t: string, size: number): number {
  return Math.max(...t.split("\n").map((l) => l.length)) * size * 0.62;
}

/** Abstand Punkt -> Strecke. */
export function distSeg(p: XY, a: XY, b: XY): number {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const l2 = dx * dx + dy * dy;
  const t = l2 ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2)) : 0;
  return Math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy);
}

export function insidePoly(p: XY, pts: XY[]): boolean {
  let inside = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [xi, yi] = pts[i]!;
    const [xj, yj] = pts[j]!;
    if (yi > p[1] !== yj > p[1] && p[0] < ((xj - xi) * (p[1] - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** Trifft (x, y) diese Grundformen? `tol` in Pixeln. */
export function hitPrims(prims: Prim[], x: number, y: number, tol = 5): boolean {
  const p: XY = [x, y];
  for (const pr of prims) {
    if (pr.kind === "text") {
      if (pr.noHit || !pr.text) continue;
      const size = pr.size ?? 12;
      const w = textWidth(pr.text, size);
      const h = pr.text.split("\n").length * size * 1.3;
      const x0 = pr.align === "right" ? pr.x! - w : pr.align === "center" ? pr.x! - w / 2 : pr.x!;
      if (x >= x0 - 3 && x <= x0 + w + 3 && y >= pr.y! - h / 2 - 3 && y <= pr.y! + h / 2 + 3) return true;
      continue;
    }
    const pts = pr.pts ?? [];
    for (let i = 0; i + 1 < pts.length; i++) if (distSeg(p, pts[i]!, pts[i + 1]!) <= tol + (pr.width ?? 1) / 2) return true;
    if (pr.kind === "poly" && pts.length > 2) {
      if (pr.closed && distSeg(p, pts[pts.length - 1]!, pts[0]!) <= tol) return true;
      if (pr.fill && insidePoly(p, pts)) return true;
    }
  }
  return false;
}
