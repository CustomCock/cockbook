import { describe, expect, it } from "vitest";
import { buildDomRows, fmtQty, rowBucketsFor } from "../src/dom";
import { HeatmapStore } from "../src/store";
import type { View } from "../src/viewport";

const view = (yLo: number, yHi: number, offset = 0): View => ({ yLo, yHi, colWidth: 2, offset, autoCenter: false });

describe("DOM", () => {
  it("groups buckets into round row sizes by zoom", () => {
    expect(rowBucketsFor(view(0, 10), 400)).toBe(1); // 40 px je Bucket
    expect(rowBucketsFor(view(0, 100), 400)).toBe(5); // 4 px je Bucket -> >= 15 px
    expect(rowBucketsFor(view(0, 1000), 400)).toBe(50);
  });

  it("splits book into bid/ask by best prices and sums visible trades", () => {
    const s = new HeatmapStore(8, 64);
    // Buckets 10..13 (bucketTicks 10): Bid bis Tick 115 (Bucket 11), Ask ab 120 (Bucket 12)
    s.pushColumn(1, true, 10, Float32Array.from([1, 2, 3, 4]));
    s.setBbo(1, 115, 120);
    s.setTrades(1, [
      { side: 1, vwapTicks: 121, qty: 5, count: 1 },
      { side: -1, vwapTicks: 112, qty: 2, count: 1 },
    ]);
    const d = buildDomRows(s, view(9, 15), 100, 600, 10);
    expect(d.rowBuckets).toBe(1);
    const at = (b: number) => d.rows.find((r) => r.bucket === b)!;
    expect(at(10).bid).toBe(1);
    expect(at(11).bid).toBe(2);
    expect(at(11).isBestBid).toBe(true);
    expect(at(12).ask).toBe(3);
    expect(at(12).isBestAsk).toBe(true);
    expect(at(12).buy).toBe(5);
    expect(at(11).sell).toBe(2);
    expect(d.k).toBe(0);
  });

  it("formats compact quantities", () => {
    expect(fmtQty(0)).toBe("");
    expect(fmtQty(0.5234)).toBe("0,523");
    expect(fmtQty(12.345)).toBe("12,35");
    expect(fmtQty(31_000)).toBe("31k");
    expect(fmtQty(-4_560_000)).toBe("-4,56M");
  });
});
