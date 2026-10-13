import { describe, expect, it } from "vitest";
import { bubbleRadius, buildBubbles, buildLines, STRIDE, VertexBuilder, type SceneParams } from "../src/scene";
import { HeatmapStore } from "../src/store";
import { priceUnit, unitToY, yToUnit, type View } from "../src/viewport";

function setup(): SceneParams {
  const store = new HeatmapStore(16, 64);
  store.pushColumn(1000, true, 130000, Float32Array.from({ length: 8 }, () => 1));
  store.setBbo(1000, 650001, 650002);
  store.setTrades(1000, [
    { side: 1, vwapTicks: 650002, qty: 4, count: 3 },
    { side: -1, vwapTicks: 650001, qty: 1, count: 1 },
  ]);
  const view: View = { yLo: 129998, yHi: 130002, colWidth: 4, offset: 0, autoCenter: true };
  return {
    store, view, pw: 800, ph: 400, bucketTicks: 5, gridStepTicks: 0, bubblePxPerUnit: 6, bubbleMaxPx: 40,
  };
}

describe("scene", () => {
  it("Trade-Bubble liegt exakt auf der Höhe ihres Preises und im richtigen Bucket", () => {
    const p = setup();
    const out = new VertexBuilder(4);
    buildBubbles(p, out);
    expect(out.n).toBe(2);
    // Reihenfolge: Verkäufe (-1) zuerst, dann Käufe
    const sellY = out.data[1]!;
    const buyY = out.data[STRIDE + 1]!;
    expect(sellY).toBeCloseTo(unitToY(priceUnit(650001, 5), p.view, p.ph));
    expect(buyY).toBeCloseTo(unitToY(priceUnit(650002, 5), p.view, p.ph));
    expect(buyY).toBeLessThan(sellY); // höherer Preis = weiter oben
    // Rückweg: Bildschirm-y -> Bucket = Bucket des Preises (650002 // 5 = 130000)
    expect(Math.floor(yToUnit(buyY, p.view, p.ph))).toBe(Math.floor(650002 / 5));
    // neueste Spalte am rechten Rand
    expect(out.data[0]).toBeCloseTo(800 - 2);
  });

  it("Bubble-Fläche wächst linear mit dem Volumen", () => {
    const r1 = bubbleRadius(1, 6, 40);
    const r4 = bubbleRadius(4, 6, 40);
    expect(r4 / r1).toBeCloseTo(2); // 4x Volumen = 2x Radius = 4x Fläche
    expect(bubbleRadius(1e6, 6, 40)).toBe(40);
    expect(bubbleRadius(0, 6, 40)).toBe(1.5);
  });

  it("BBO-Linien liegen auf Bid- und Ask-Preis", () => {
    const p = setup();
    const out = new VertexBuilder(4);
    buildLines(p, out);
    const ys = new Set<number>();
    for (let i = 0; i < out.n; i++) ys.add(Number(out.data[i * STRIDE + 1]!.toFixed(4)));
    expect(ys).toContain(Number(unitToY(priceUnit(650001, 5), p.view, p.ph).toFixed(4)));
    expect(ys).toContain(Number(unitToY(priceUnit(650002, 5), p.view, p.ph).toFixed(4)));
  });

  it("Bubbles außerhalb des Sichtbereichs werden ausgelassen", () => {
    const p = setup();
    p.view = { ...p.view, yLo: 140000, yHi: 140010 };
    const out = new VertexBuilder(4);
    buildBubbles(p, out);
    expect(out.n).toBe(0);
  });
});
