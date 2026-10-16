import { describe, expect, it } from "vitest";
import { aggregate, buildCandles, CandleSeries, RRectBuilder, RRECT_STRIDE, timeToX, type Bar } from "../src/candles";
import { HeatmapStore } from "../src/store";

const M = 60_000;
const bar = (min: number, o: number, h: number, l: number, c: number, v = 1): Bar => ({ t: min * M, o, h, l, c, v });

describe("Kerzen", () => {
  it("10m aus 1m: ausgerichtet, open = erste, close = letzte, Hoch/Tief/Volumen korrekt", () => {
    const ones = [
      bar(8, 1, 2, 0.5, 1.5), // gehört zur 0-10-Kerze
      bar(9, 1.5, 3, 1.4, 2),
      bar(10, 2, 2.5, 1.9, 2.2), // neue 10-20-Kerze
      bar(19, 2.2, 4, 1, 3),
      bar(20, 3, 3, 3, 3),
    ];
    const tens = aggregate(ones, 10);
    expect(tens.map((b) => b.t / M)).toEqual([0, 10, 20]);
    expect(tens[0]).toEqual({ t: 0, o: 1, h: 3, l: 0.5, c: 2, v: 2 });
    expect(tens[1]).toEqual({ t: 10 * M, o: 2, h: 4, l: 1, c: 3, v: 2 });
    expect(aggregate(ones, 1)).toBe(ones);
  });

  it("3m-Ausrichtung auf Vielfache von 3 Minuten", () => {
    const threes = aggregate([bar(2, 1, 1, 1, 1), bar(3, 1, 1, 1, 1), bar(5, 1, 1, 1, 1), bar(6, 1, 1, 1, 1)], 3);
    expect(threes.map((b) => b.t / M)).toEqual([0, 3, 6]);
  });

  it("Serie: Live-Update ersetzt laufende Kerze, Sortierung", () => {
    const s = new CandleSeries();
    s.setHistory([
      [2 * M, 1, 1, 1, 1, 1],
      [1 * M, 1, 1, 1, 1, 1],
    ]);
    s.upsert(bar(2, 1, 5, 1, 4));
    expect(s.all().map((b) => b.t)).toEqual([M, 2 * M]);
    expect(s.all()[1]!.h).toBe(5);
    expect(s.first).toBe(M);
  });

  it("Kerze liegt auf der Zeitachse der Heatmap und im Preis richtig", () => {
    const store = new HeatmapStore(8, 64);
    const now = 100 * M;
    store.pushColumn(now, true, 1000, Float32Array.from([1]));
    const view = { yLo: 990, yHi: 1010, colWidth: 2, offset: 0, autoCenter: true };
    const scene = { store, view, pw: 600, ph: 200, intervalMs: 100, tickSize: 0.1, bucketTicks: 1 };
    expect(timeToX(now, scene)).toBe(600); // neueste Spalte endet am rechten Rand
    expect(timeToX(now - 1000, scene)).toBe(580); // 1 s = 10 Spalten à 2 px
    const out = new RRectBuilder();
    // 1m-Kerze, die gerade läuft: 99:00–100:00; Preis 100.0 = Tick 1000 = Mitte des Sichtbereichs
    buildCandles([bar(99, 100, 100.5, 99.5, 100)], 1, scene, out);
    expect(out.n).toBe(12); // Docht + Körper, je 6 Vertices
    // Mittelpunkt des Körpers (2. Rechteck): Vertex-Position minus lokale Verschiebung
    const o = 6 * RRECT_STRIDE;
    const cy = out.data[o + 1]! - out.data[o + 3]!;
    expect(cy).toBeCloseTo(100); // y-Mitte des Plots
  });
});
