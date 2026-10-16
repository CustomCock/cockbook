import { describe, expect, it } from "vitest";
import { decimalsOf, Markets } from "../src/markets";

describe("Markets", () => {
  it("Preis, 24h-Änderung und Nachkommastellen", () => {
    const m = new Markets();
    m.ensure("XRPUSDT", "0.0001");
    m.apply({ t: "tick", sym: "XRPUSDT", ts: 1, last: 2.2, open: 2.0, high: 2.3, low: 1.9, vol: 1, qvol: 1 });
    expect(m.price("XRPUSDT")).toBe(2.2);
    expect(m.change24h("XRPUSDT")).toBeCloseTo(10);
    expect(m.bySymbol.get("XRPUSDT")!.decimals).toBe(4);
    expect(decimalsOf("0.10")).toBe(1);
  });

  it("OI-Veränderung erst mit genug Verlauf", () => {
    const m = new Markets();
    const min = 60_000;
    m.apply({ t: "oi", sym: "BTCUSDT", ts: 0, oi: 100 });
    expect(m.oiChange("BTCUSDT", 5)).toBeNull();
    m.apply({ t: "oi", sym: "BTCUSDT", ts: 3 * min, oi: 101 });
    m.apply({ t: "oi", sym: "BTCUSDT", ts: 6 * min, oi: 102 });
    expect(m.oiChange("BTCUSDT", 5)).toBeCloseTo(2); // gegen ts=0 (älter als 5 min)
    expect(m.oiChange("BTCUSDT", 60)).toBeNull();
  });

  it("Liquidationen: dedupliziert, neueste zuerst, Summen je Seite", () => {
    const m = new Markets();
    const liq = { t: "liq" as const, sym: "ETHUSDT", ts: 1000, side: -1 as const, price: 3000, qty: 2 };
    m.apply(liq);
    m.apply(liq); // erneut gesendet nach Reconnect
    m.apply({ ...liq, ts: 2000, side: 1, qty: 1 });
    expect(m.liqs.length).toBe(2);
    expect(m.liqs[0]!.ts).toBe(2000);
    expect(m.liqSums(2000)).toEqual({ long: 6000, short: 3000 });
    expect(m.liqSums(2000, "BTCUSDT")).toEqual({ long: 0, short: 0 });
  });
});
