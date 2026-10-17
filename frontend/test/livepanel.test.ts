import { describe, expect, it } from "vitest";
import { liveLines } from "../src/livepanel";
import type { LiveState } from "../src/net";

describe("liveLines", () => {
  it("zeigt Position, Liquidation, TP/SL und Orders ohne Ziehen/Löschen", () => {
    const s: LiveState = {
      type: "live_state", exchange: "bitunix", status: "ok", message: "", ws: true, key: "…abcd",
      updated: 0, read_only: true, max_leverage: 100,
      positions: [{ symbol: "BITUNIX:BTCUSDT", position_id: "1", qty: -0.5, entry: 60000, mark: 59900, upnl: 50,
        roe: 10, realized: 0, fee: 0.1, funding: 0, leverage: 100, margin: 300, margin_mode: "ISOLATION",
        liq: 60500, tp: 59000, sl: null, opened: 0 }],
      orders: [{ id: "9", symbol: "BITUNIX:BTCUSDT", side: "buy", kind: "limit", qty: 1, filled: 0.25, price: 58000,
        reduce_only: true, tp: null, sl: null, status: "NEW", created: 0 }],
    };
    const lines = liveLines("BITUNIX:BTCUSDT", s);
    expect(lines.map((l) => l.kind)).toEqual(["entry", "liq", "tp", "order"]);
    expect(lines[0]!.label).toBe("LIVE Short 0,5 · +50,00 $");
    expect(lines[2]!.label).toBe("LIVE TP +500,00 $");
    expect(lines[3]!.label).toBe("LIVE Limit Kauf 0,75 RO");
    expect(lines.every((l) => !l.drag && !l.cancel)).toBe(true);
    expect(liveLines("BTCUSDT", s)).toEqual([]);
    // freigeschaltet: Orders und TP/SL ziehbar bzw. löschbar
    const edit = liveLines("BITUNIX:BTCUSDT", s, true);
    expect(edit[2]!.drag).toEqual({ type: "tp", symbol: "BITUNIX:BTCUSDT" });
    expect(edit[3]!.drag).toEqual({ type: "order", id: "9", field: "price" });
    expect(edit[0]!.drag).toBeUndefined(); // Einstieg/Liquidation nie
    expect(liveLines("X", null)).toEqual([]);
  });
});
