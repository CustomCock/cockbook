import { describe, expect, it } from "vitest";
import { TabModel } from "../src/tabs";

describe("TabModel", () => {
  it("opens new tabs right of the active one and activates them", () => {
    const m = new TabModel();
    m.open("BTC");
    m.open("ETH");
    m.open("BTC"); // schon offen -> nur aktivieren
    expect(m.tabs).toEqual(["BTC", "ETH"]);
    expect(m.active).toBe("BTC");
    m.open("SOL");
    expect(m.tabs).toEqual(["BTC", "SOL", "ETH"]);
  });

  it("closing the active tab activates the right neighbour, never closes the last", () => {
    const m = new TabModel();
    for (const s of ["A", "B", "C"]) m.open(s);
    m.active = "B";
    expect(m.close("B")).toBe("C");
    expect(m.close("C")).toBe("A");
    expect(m.close("A")).toBe("A");
    expect(m.tabs).toEqual(["A"]);
  });

  it("moves, steps cyclically and drops unavailable symbols", () => {
    const m = new TabModel();
    m.tabs = ["A", "B", "C"];
    m.active = "C";
    m.move("C", 0);
    expect(m.tabs).toEqual(["C", "A", "B"]);
    expect(m.step(-1)).toBe("B");
    expect(m.step(1)).toBe("A");
    m.retain(new Set(["A", "B"]));
    expect(m.tabs).toEqual(["A", "B"]);
    expect(m.active).toBe("A");
  });
});
