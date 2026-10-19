import { describe, expect, it } from "vitest";
import {
  bubbleRadius,
  buildBbo,
  buildBubbles,
  buildLiquidations,
  STRIDE,
  VertexBuilder,
  type SceneParams,
} from "../src/scene";
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
  store.setLiquidations(1000, [{ side: -1, priceTicks: 650000, qty: 2 }]);
  const view: View = { yLo: 129998, yHi: 130002, colWidth: 4, offset: 0, autoCenter: true };
  return {
    store,
    view,
    pw: 800,
    ph: 400,
    bucketTicks: 5,
    tickSize: 0.1,
    gridStepTicks: 0,
    bubblePxPerSqrtKusd: 1.2,
    bubbleMaxPx: 40,
  };
}

describe("scene", () => {
  it("nach rechts gescrollt (offset < 0): Daten rücken nach links, nichts aus dem Ringpuffer-Umlauf", () => {
    const p = setup();
    // alter Trade in einer weiter zurückliegenden Spalte (Index -1 würde ohne Schutz hierhin laufen)
    for (let i = 1; i < 16; i++) p.store.pushColumn(1000 + i * 100, true, 130000, Float32Array.from({ length: 8 }, () => 1));
    p.store.setTrades(1100, [{ side: 1, vwapTicks: 650002, qty: 4, count: 1 }]);
    const live = new VertexBuilder(64);
    buildBubbles(p, live);
    p.view = { ...p.view, offset: -50 };
    const out = new VertexBuilder(64);
    buildBubbles(p, out);
    // gleiche Bubbles (keine Kopien aus k < 0), um 50 Spalten à 4 px nach links verschoben
    expect(out.n).toBe(live.n);
    for (let i = 0; i < out.n; i++) expect(out.data[i * STRIDE]).toBeCloseTo(live.data[i * STRIDE]! - 200);
  });

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
    expect(Math.floor(yToUnit(buyY, p.view, p.ph))).toBe(Math.floor(650002 / 5));
    expect(out.data[0]).toBeCloseTo(800 - 2); // neueste Spalte am rechten Rand
  });

  it("Bubble-Größe nach USDT-Gegenwert: Fläche linear, über Coins vergleichbar", () => {
    const r1 = bubbleRadius(10_000, 1.2, 40);
    const r4 = bubbleRadius(40_000, 1.2, 40);
    expect(r4 / r1).toBeCloseTo(2); // 4x Gegenwert = 2x Radius = 4x Fläche
    expect(bubbleRadius(1e12, 1.2, 40)).toBe(40);
    expect(bubbleRadius(0, 1.2, 40)).toBe(1.5);
    // 4 BTC à 65 000,20 = 260 kUSDT
    const p = setup();
    const out = new VertexBuilder(4);
    buildBubbles(p, out);
    expect(out.data[STRIDE + 2]).toBeCloseTo(bubbleRadius(4 * 650002 * 0.1, 1.2, 40));
  });

  it("BBO-Bänder liegen mittig auf Bid- und Ask-Preis", () => {
    const p = setup();
    const out = new VertexBuilder(4);
    buildBbo(p, out);
    // Mitte jedes waagerechten Bands = Mittelwert der y-Koordinaten seiner 6 Vertices
    const centers = new Set<number>();
    for (let q = 0; q < out.n / 6; q++) {
      let sum = 0;
      for (let i = 0; i < 6; i++) sum += out.data[(q * 6 + i) * STRIDE + 1]!;
      centers.add(Number((sum / 6).toFixed(3)));
    }
    expect(centers).toContain(Number(unitToY(priceUnit(650001, 5), p.view, p.ph).toFixed(3)));
    expect(centers).toContain(Number(unitToY(priceUnit(650002, 5), p.view, p.ph).toFixed(3)));
  });

  it("Liquidationen: Position am Preis, Farbe nach Seite", () => {
    const p = setup();
    const out = new VertexBuilder(4);
    buildLiquidations(p, out);
    expect(out.n).toBe(1);
    expect(out.data[1]).toBeCloseTo(unitToY(priceUnit(650000, 5), p.view, p.ph));
    expect(out.data[3]).toBe(1.0); // orange (Long liquidiert)
  });

  it("Bubbles außerhalb des Sichtbereichs werden ausgelassen", () => {
    const p = setup();
    p.view = { ...p.view, yLo: 140000, yHi: 140010 };
    const out = new VertexBuilder(4);
    buildBubbles(p, out);
    expect(out.n).toBe(0);
  });
});
