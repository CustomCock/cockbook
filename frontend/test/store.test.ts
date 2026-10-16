import { describe, expect, it } from "vitest";
import { HeatmapStore, rowsFor } from "../src/store";

const col = (...v: number[]) => Float32Array.from(v);

describe("HeatmapStore", () => {
  it("platziert die erste Spalte mittig und liest Werte absolut", () => {
    const s = new HeatmapStore(8, 16);
    s.pushColumn(1000, true, 100, col(1, 2, 3, 4));
    expect(s.origin).toBe(100 + 2 - 8);
    expect(s.valueAt(0, 100)).toBe(1);
    expect(s.valueAt(0, 103)).toBe(4);
    expect(s.valueAt(0, 104)).toBe(0);
    expect(s.valueAt(1, 100)).toBe(0); // existiert nicht
  });

  it("zählt Spalten rückwärts von der neuesten und überschreibt im Ring", () => {
    const s = new HeatmapStore(3, 16);
    for (let i = 0; i < 5; i++) s.pushColumn(i, true, 100, col(i + 1));
    expect(s.count).toBe(3);
    expect([0, 1, 2].map((k) => s.valueAt(k, 100))).toEqual([5, 4, 3]);
    expect(s.ts[s.latest]).toBe(4);
  });

  it("verschiebt origin, wenn der Preis aus dem Bereich läuft, und behält alte Daten", () => {
    const s = new HeatmapStore(4, 16);
    s.pushColumn(0, true, 100, col(7, 8));
    const o1 = s.origin;
    s.pushColumn(1, true, o1 + 20, col(9)); // weit über dem Bereich
    expect(s.origin).not.toBe(o1);
    expect(s.fullDirty).toBe(true);
    expect(s.valueAt(0, o1 + 20)).toBe(9);
    // alte Spalte: Werte außerhalb des neuen Bereichs sind weg, Rest korrekt verschoben
    if (100 >= s.origin) expect(s.valueAt(1, 100)).toBe(7);
  });

  it("re-origin nach unten verschiebt korrekt", () => {
    const s = new HeatmapStore(2, 8);
    s.pushColumn(0, true, 10, col(1, 2)); // origin = 10 + 1 - 4 = 7
    s.reorigin(5);
    expect(s.valueAt(0, 10)).toBe(1);
    expect(s.valueAt(0, 11)).toBe(2);
    expect(s.valueAt(0, 12)).toBe(0);
  });

  it("nicht synchronisierte Spalte ist leer und markiert", () => {
    const s = new HeatmapStore(4, 16);
    s.pushColumn(0, true, 100, col(1));
    s.pushColumn(1, false, 0, col());
    expect(s.synced[s.latest]).toBe(0);
    expect(s.valueAt(0, 100)).toBe(0);
    expect(s.valueAt(1, 100)).toBe(1);
  });

  it("BBO und Trades hängen an der Spalte mit gleichem Zeitstempel", () => {
    const s = new HeatmapStore(4, 16);
    s.pushColumn(500, true, 100, col(1));
    s.setBbo(500, 1001, 1002);
    s.setBbo(400, 1, 2); // veraltet -> ignoriert
    s.setTrades(500, [{ side: 1, vwapTicks: 1002, qty: 1, count: 1 }]);
    expect(s.bestBid[s.latest]).toBe(1001);
    expect(s.trades[s.latest]?.length).toBe(1);
  });

  it("ignoriert erneut gesendete (ältere) Spalten nach einem Reconnect", () => {
    const s = new HeatmapStore(8, 16);
    s.pushColumn(100, true, 100, col(1));
    s.pushColumn(200, true, 100, col(2));
    expect(s.pushColumn(100, true, 100, col(9))).toBe(-1);
    expect(s.pushColumn(200, true, 100, col(9))).toBe(-1);
    expect(s.count).toBe(2);
    expect(s.valueAt(0, 100)).toBe(2);
    s.setBbo(100, 5, 6); // gehört zu keiner aktuellen Spalte -> ignoriert
    expect(Number.isNaN(s.bestBid[s.latest]!)).toBe(true);
  });

  it("rowsFor: Zweierpotenz, mind. 2.5x halbe Spaltenhöhe, begrenzt", () => {
    expect(rowsFor(600, 16384)).toBe(2048);
    expect(rowsFor(100, 16384)).toBe(512);
    expect(rowsFor(1000, 2048)).toBe(2048);
    expect(rowsFor(1500, 16384, 2048)).toBe(2048);
  });

  it("füllt Zeitlücken mit Lücken-Spalten (Zeitachse bleibt korrekt)", () => {
    const s = new HeatmapStore(8, 16, 100);
    s.pushColumn(1000, true, 100, col(1));
    s.pushColumn(1400, true, 100, col(2)); // 3 Spalten fehlen
    expect(s.count).toBe(5);
    expect(s.valueAt(0, 100)).toBe(2);
    expect(s.valueAt(4, 100)).toBe(1);
    expect(s.synced[s.indexAgo(2)]).toBe(2);
    expect(s.ts[s.indexAgo(1)]).toBe(1300);
    expect(s.oldestTs).toBe(1000);
    s.pushColumn(100_000, true, 100, col(3)); // riesige Lücke: höchstens ein Ring voll
    expect(s.count).toBe(8);
    expect(s.ts[s.indexAgo(7)]).toBe(100_000 - 7 * 100);
    expect(s.fullDirty).toBe(true);
  });
});
