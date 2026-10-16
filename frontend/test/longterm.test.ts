import { describe, expect, it } from "vitest";
import { coarseColAt, coarseView } from "../src/longterm";
import { colToX, type View } from "../src/viewport";

const P = { fineIntervalMs: 100, longIntervalMs: 2000, factor: 2 };
const view: View = { yLo: 100, yHi: 300, colWidth: 2, offset: 0, autoCenter: true };

describe("coarseView", () => {
  it("maps the same point in time to the same x", () => {
    const fineLatest = 1_000_000;
    const coarseLatest = 999_000; // grobe Spalte endet 1 s vor der neuesten feinen
    const cv = coarseView(view, fineLatest, coarseLatest, P);
    expect(cv.colWidth).toBe(40);
    expect(cv.yLo).toBe(50);
    expect(cv.yHi).toBe(150);
    const pw = 1000;
    // Rechte Kante der groben Spalte j=0 (Zeit coarseLatest) == Rechte Kante feiner Spalte k=10
    const xFine = colToX(10, view, pw) + view.colWidth / 2;
    const xCoarse = colToX(0, cv, pw) + cv.colWidth / 2;
    expect(xCoarse).toBeCloseTo(xFine, 9);
  });

  it("follows scrolling back in time", () => {
    const cv = coarseView({ ...view, offset: 600 }, 1_000_000, 1_000_000, P);
    expect(cv.offset).toBe(30); // 600 x 100 ms = 60 s = 30 grobe Spalten
  });

  it("finds the coarse column under a fine column", () => {
    expect(coarseColAt(0, 1_000_000, 999_000, P)).toBe(-1); // neuer als die letzte grobe
    expect(coarseColAt(10, 1_000_000, 999_000, P)).toBe(0);
    expect(coarseColAt(29, 1_000_000, 999_000, P)).toBe(0);
    expect(coarseColAt(30, 1_000_000, 999_000, P)).toBe(1);
  });
});
