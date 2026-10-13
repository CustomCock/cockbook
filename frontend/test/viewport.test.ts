import { describe, expect, it } from "vitest";
import { colToX, priceUnit, recenter, unitToY, xToCol, yToUnit, zoomY, type View } from "../src/viewport";

const v: View = { yLo: 100, yHi: 200, colWidth: 2, offset: 0, autoCenter: true };

describe("viewport", () => {
  it("y-Mapping ist umkehrbar und oben = hoher Preis", () => {
    expect(unitToY(200, v, 500)).toBe(0);
    expect(unitToY(100, v, 500)).toBe(500);
    expect(yToUnit(unitToY(137.25, v, 500), v, 500)).toBeCloseTo(137.25);
  });

  it("Preis liegt in der Mitte seines Ticks innerhalb des Buckets", () => {
    // Bucket 5 Ticks: Tick 650001 liegt in Bucket 130000, Position 130000.3
    expect(priceUnit(650001, 5)).toBeCloseTo(130000.3);
    expect(Math.floor(priceUnit(650004, 5))).toBe(130000);
    expect(Math.floor(priceUnit(650005, 5))).toBe(130001);
  });

  it("neueste Spalte rechts, x <-> Spalte konsistent", () => {
    expect(colToX(0, v, 1000)).toBe(999);
    expect(xToCol(999, v, 1000)).toBe(0);
    expect(xToCol(colToX(37, v, 1000), v, 1000)).toBe(37);
    expect(xToCol(colToX(0, { ...v, offset: 10 }, 1000), { ...v, offset: 10 }, 1000)).toBe(0);
  });

  it("Zoom hält den Ankerpunkt fest", () => {
    const z = zoomY(v, 0.5, 150);
    expect(z.yHi - z.yLo).toBeCloseTo(50);
    expect((150 - z.yLo) / (z.yHi - z.yLo)).toBeCloseTo(0.5);
  });

  it("Recentering nur außerhalb der Ränder", () => {
    expect(recenter(v, 150, 0.15)).toBe(v);
    const r = recenter(v, 195, 0.15);
    expect((r.yLo + r.yHi) / 2).toBe(195);
    expect(r.yHi - r.yLo).toBe(100);
  });
});
