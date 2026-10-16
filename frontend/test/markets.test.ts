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

describe("markets: symbol info, sessions, daily RSI", () => {
  it("stores kind/label and market sessions", () => {
    const m = new Markets();
    m.setInfo({ symbol: "XAUUSDT", tick_size: "0.01", kind: "tradfi", label: "Gold (XAU)" });
    expect(m.bySymbol.get("XAUUSDT")!.kind).toBe("tradfi");
    expect(m.bySymbol.get("XAUUSDT")!.label).toBe("Gold (XAU)");
    expect(m.ensure("BTCUSDT").label).toBe("BTC");
    m.apply({ t: "sess", sym: "COMMODITY", ts: 1, session: "NO_TRADING", start: 0, end: 10 });
    expect(m.sessions.get("COMMODITY")!.session).toBe("NO_TRADING");
    expect(m.bySymbol.has("COMMODITY")).toBe(false); // kein Coin
  });

  it("computes daily RSI with the running day replaced by the live price", () => {
    const m = new Markets();
    const day = 86_400_000;
    const now = 100 * day + 1000;
    // 30 Tage stetig steigend -> RSI 100; heute fällt der Live-Preis stark -> RSI < 100
    const rows = Array.from({ length: 30 }, (_, i) => [(71 + i) * day, 1, 1, 1, 10 + i, 1] as [number, number, number, number, number, number]);
    m.apply({ t: "d1", sym: "BTCUSDT", x: rows });
    expect(m.rsi1d("BTCUSDT", now)).toBe(100);
    m.apply({ t: "tick", sym: "BTCUSDT", ts: now, last: 20, open: 39, high: 39, low: 20, vol: 1, qvol: 1 });
    const r = m.rsi1d("BTCUSDT", now)!;
    expect(r).toBeLessThan(70);
  });
});
