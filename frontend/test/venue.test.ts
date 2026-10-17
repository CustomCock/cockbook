import { describe, expect, it } from "vitest";
import { coinOf, keyFor, legsOf, venueOf } from "../src/venue";

describe("venue keys", () => {
  it("liest Börse und Coin", () => {
    expect(venueOf("BTCUSDT")).toBe("binance");
    expect(venueOf("BITUNIX:BTCUSDT")).toBe("bitunix");
    expect(venueOf("MULTI:ETHUSDT")).toBe("multi");
    expect(coinOf("MULTI:ETHUSDT")).toBe("ETHUSDT");
    expect(coinOf("ETHUSDT")).toBe("ETHUSDT");
    expect(keyFor("bitunix", "SOLUSDT")).toBe("BITUNIX:SOLUSDT");
    expect(keyFor("binance", "SOLUSDT")).toBe("SOLUSDT");
    expect(legsOf("MULTI:XUSDT")).toEqual(["XUSDT", "BITUNIX:XUSDT"]);
    expect(legsOf("XUSDT")).toEqual(["XUSDT"]);
  });
});
