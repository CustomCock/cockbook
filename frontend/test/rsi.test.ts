import { describe, expect, it } from "vitest";
import { lastRsi, rsiSeries, rsiZone } from "../src/rsi";

describe("rsi", () => {
  it("matches Wilder's original worked example", () => {
    // Beispieldaten aus Wilders "New Concepts in Technical Trading Systems" (1978),
    // wie sie in vielen Lehrbüchern nachgerechnet werden; erster RSI(14) ~ 70.53
    const closes = [
      44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.1, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
      46.0, 46.03, 46.41, 46.22, 45.64,
    ];
    const s = rsiSeries(closes, 14);
    for (let i = 0; i < 14; i++) expect(Number.isNaN(s[i]!)).toBe(true);
    expect(s[14]).toBeCloseTo(70.46, 1);
    expect(s[15]).toBeCloseTo(66.25, 1);
    expect(s[19]).toBeCloseTo(57.92, 0);
  });

  it("handles flat and one-sided series", () => {
    expect(lastRsi(new Array(20).fill(5))).toBe(50);
    expect(lastRsi(Array.from({ length: 20 }, (_, i) => i))).toBe(100);
    expect(lastRsi(Array.from({ length: 20 }, (_, i) => 20 - i))).toBe(0);
    expect(lastRsi([1, 2, 3])).toBeNull();
  });

  it("classifies zones", () => {
    expect(rsiZone(75)).toBe("overbought");
    expect(rsiZone(30)).toBe("oversold");
    expect(rsiZone(50)).toBe("neutral");
    expect(rsiZone(null)).toBe("neutral");
  });
});
