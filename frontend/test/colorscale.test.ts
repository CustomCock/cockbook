import { describe, expect, it } from "vitest";
import { buildLut, computeRange, normalize, percentile } from "../src/colorscale";

describe("colorscale", () => {
  it("percentile interpoliert", () => {
    const s = [1, 2, 3, 4, 5];
    expect(percentile(s, 0)).toBe(1);
    expect(percentile(s, 1)).toBe(5);
    expect(percentile(s, 0.5)).toBe(3);
    expect(percentile(s, 0.125)).toBe(1.5);
  });

  it("log-Skala: Verhältnisse bleiben Abstände", () => {
    const r = computeRange(Float32Array.from({ length: 1000 }, (_, i) => 1 + i), { contrast: 1, threshold: 0 })!;
    const a = normalize(100, r) - normalize(50, r);
    const b = normalize(800, r) - normalize(400, r);
    expect(a).toBeCloseTo(b, 5); // 2x mehr = gleicher Farbschritt
  });

  it("Schwelle blendet kleine Werte aus, Kontrast sättigt früher", () => {
    const vals = Float32Array.from({ length: 1000 }, (_, i) => 1 + i);
    const r = computeRange(vals, { contrast: 1, threshold: 0.5 })!;
    expect(normalize(100, r)).toBe(-1);
    const hi = computeRange(vals, { contrast: 4, threshold: 0 })!;
    expect(normalize(300, hi)).toBe(1);
    expect(normalize(300, computeRange(vals, { contrast: 1, threshold: 0 })!)).toBeLessThan(1);
  });

  it("zu wenig Daten -> keine Skala", () => {
    expect(computeRange(Float32Array.from([1, 2]), { contrast: 1, threshold: 0 })).toBeNull();
  });

  it("LUT ist 256 RGBA, dunkel -> hell", () => {
    const lut = buildLut();
    expect(lut.length).toBe(1024);
    const lum = (i: number) => lut[i * 4]! + lut[i * 4 + 1]! + lut[i * 4 + 2]!;
    expect(lum(255)).toBeGreaterThan(lum(128));
    expect(lum(128)).toBeGreaterThan(lum(0));
  });
});
