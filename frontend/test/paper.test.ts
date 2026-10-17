import { describe, expect, it } from "vitest";
import { bracketInfo, clickOrder, floorStep, qtyForPercent, qtyFromInput, roundTick, tradeLines } from "../src/paper";
import type { PaperOrder, PaperPosition } from "../src/net";

describe("paper helpers", () => {
  it("rundet auf Tick und Schrittweite", () => {
    expect(roundTick(81234.34999, 0.1)).toBe(81234.3);
    expect(roundTick(0.123456, 0.0001)).toBe(0.1235);
    expect(floorStep(0.0019999, 0.001)).toBe(0.001);
    expect(floorStep(0.003, 0.001)).toBe(0.003);
  });

  it("Klick-Trading wählt Seite und Ordertyp", () => {
    expect(clickOrder("bid", 99, 100, 101)).toEqual({ side: "buy", kind: "limit" });
    expect(clickOrder("bid", 102, 100, 101)).toEqual({ side: "buy", kind: "stop" });
    expect(clickOrder("ask", 103, 100, 101)).toEqual({ side: "sell", kind: "limit" });
    expect(clickOrder("ask", 99, 100, 101)).toEqual({ side: "sell", kind: "stop" });
    expect(clickOrder("chart", 99, 100, 101)).toEqual({ side: "buy", kind: "limit" });
    expect(clickOrder("chart", 105, 100, 101)).toEqual({ side: "sell", kind: "limit" });
  });

  it("rechnet Mengen um", () => {
    expect(qtyFromInput(1000, "usdt", 50000, 0.001)).toBe(0.02);
    expect(qtyFromInput(0.0257, "coin", 50000, 0.001)).toBe(0.025);
    expect(qtyFromInput(-1, "coin", 1, 0.001)).toBe(0);
    // 100 % von 1000 USDT bei 10x, ohne Gebühr = 10 000 USDT Nominal
    expect(qtyForPercent(100, 1000, 10, 100, 0.01, 0)).toBe(100);
    expect(qtyForPercent(50, 1000, 10, 100, 0.01, 0)).toBe(50);
    expect(qtyForPercent(100, 1000, 10, 100, 0.01, 0.001)).toBeLessThan(100);
  });

  it("zeigt Chance/Risiko", () => {
    const b = bracketInfo("buy", 100, 2, 110, 95);
    expect(b.tpPnl).toBe(20);
    expect(b.slPnl).toBe(-10);
    expect(b.rr).toBe(2);
    expect(b.tpPct).toBeCloseTo(10);
    const s = bracketInfo("sell", 100, 1, 90, null);
    expect(s.tpPnl).toBe(10);
    expect(s.rr).toBeNull();
  });

  it("baut Linien für Position und Orders", () => {
    const pos: PaperPosition = {
      symbol: "X", qty: -2, entry: 100, mark: 99, upnl: 2, roe: 10, leverage: 10, margin: 20,
      liq: 109.5, tp: 90, sl: 105, opened: 0, breakeven: 99.9,
    };
    const order: PaperOrder = {
      id: "P1", symbol: "X", side: "buy", kind: "limit", qty: 1, filled: 0, price: 95, trigger: null,
      reduce_only: true, tp: null, sl: null, queue: 3, created: 0,
    };
    const lines = tradeLines("X", [pos], [order, { ...order, id: "P2", symbol: "Y" }]);
    expect(lines.map((l) => l.kind)).toEqual(["entry", "breakeven", "liq", "tp", "sl", "order"]);
    expect(lines[0]!.label).toContain("Short 2");
    expect(lines[3]!.label).toBe("TP +20,00 $");
    expect(lines[4]!.label).toBe("SL -10,00 $");
    expect(lines[5]!.label).toBe("Limit Kauf 1 RO · 3 vor dir");
    expect(lines[5]!.drag).toEqual({ type: "order", id: "P1", field: "price" });
  });
});
