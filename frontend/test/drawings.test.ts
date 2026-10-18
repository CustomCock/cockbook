import { describe, expect, it } from "vitest";
import { DrawingController, type DrawingSync } from "../src/drawings/controller";
import { build, hitPrims, type Ctx } from "../src/drawings/geom";
import { drawingKey, styleFor, TOOLS, type Drawing, type Pt } from "../src/drawings/model";

const M = 60_000;
const T0 = 1_700_000_000_000;

/** 1 px = 1 s, 1 px = 0,1 Preis; Plot 1000 x 500, rechts = T0 + 1000 s, oben = Preis 125. */
function ctx(): Ctx {
  const bars = Array.from({ length: 30 }, (_, i) => ({ t: T0 + i * M, o: 100 + i * 0.1, h: 101 + i * 0.1, l: 99 + i * 0.1, c: 100.5 + i * 0.1, v: 10 }));
  return {
    toX: (t) => (t - T0) / 1000,
    toT: (x) => T0 + x * 1000,
    toY: (p) => (125 - p) * 10,
    toP: (y) => 125 - y / 10,
    pw: 1000,
    ph: 500,
    interval: 100,
    bars,
    trades: () => [
      { p: 100, q: 2 },
      { p: 101, q: 1 },
    ],
    fmtP: (p) => p.toFixed(1),
    fmtT: (t) => new Date(t).toISOString(),
    tick: 0.1,
  };
}

function ptsFor(n: number): Pt[] {
  // Zickzack, damit Muster/Kanäle nicht entarten
  return Array.from({ length: n }, (_, i) => ({ t: T0 + (i + 1) * 90_000, p: 100 + (i % 2 ? 5 : 0) + i }));
}

function drawing(tool: string, pts: Pt[]): Drawing {
  return { id: `x-${tool}`, tool: tool as Drawing["tool"], pts, style: styleFor(tool, { text: "Hallo" }), created: 0 };
}

const finite = (v: number | undefined) => v === undefined || Number.isFinite(v);

describe("Zeichnungen – Geometrie", () => {
  it("jedes Werkzeug liefert endliche Grundformen", () => {
    const c = ctx();
    for (const t of TOOLS) {
      const n = t.id === "long" || t.id === "short" ? 3 : t.id === "gannsqfixed" ? 2 : typeof t.points === "number" ? t.points : 6;
      const prims = build(drawing(t.id, ptsFor(n)), c);
      expect(prims.length, t.id).toBeGreaterThan(0);
      for (const p of prims) {
        expect(finite(p.x) && finite(p.y), `${t.id} text`).toBe(true);
        for (const [x, y] of p.pts ?? []) expect(Number.isFinite(x) && Number.isFinite(y), t.id).toBe(true);
      }
    }
  });

  it("Werkzeuge vertragen unvollständige Entwürfe (beim Ziehen des 2. Punkts)", () => {
    const c = ctx();
    for (const t of TOOLS) {
      expect(() => build(drawing(t.id, ptsFor(2)), c), t.id).not.toThrow();
    }
  });

  it("Klickfläche: auf der Linie ja, daneben nein", () => {
    const c = ctx();
    const prims = build(drawing("trend", [{ t: T0, p: 120 }, { t: T0 + 500_000, p: 110 }]), c);
    // Linie von (0,50) nach (500,150): Mitte (250,100)
    expect(hitPrims(prims, 250, 101)).toBe(true);
    expect(hitPrims(prims, 250, 140)).toBe(false);
  });

  it("Fib-Retracement beschriftet die Stufen mit Preisen", () => {
    const prims = build(drawing("fibret", [{ t: T0, p: 100 }, { t: T0 + 300_000, p: 110 }]), ctx());
    const labels = prims.filter((p) => p.kind === "text").map((p) => p.text ?? "");
    expect(labels.some((l) => l.includes("0.618") && l.includes("103.8"))).toBe(true);
  });

  it("Schlüssel je Coin: Börse und Ansicht fallen weg", () => {
    expect(drawingKey("BTCUSDT")).toBe("BTCUSDT");
    expect(drawingKey("BITUNIX:BTCUSDT")).toBe("BTCUSDT");
    expect(drawingKey("MULTI:ETHUSDT@long")).toBe("ETHUSDT");
  });
});

class FakeSync implements DrawingSync {
  ops: string[] = [];
  put(key: string, d: Drawing) {
    this.ops.push(`put ${key} ${d.tool}`);
  }
  remove(key: string, id: string) {
    this.ops.push(`remove ${key} ${id}`);
  }
  clear(key: string) {
    this.ops.push(`clear ${key}`);
  }
  load(key: string) {
    this.ops.push(`load ${key}`);
  }
}

const ev = (extra: Partial<PointerEvent> = {}) => ({ shiftKey: false, buttons: 1, ...extra }) as PointerEvent;

describe("Zeichnungen – Steuerung", () => {
  it("Trendlinie mit zwei Klicks, auswählen, verschieben, rückgängig", () => {
    const sync = new FakeSync();
    const c = new DrawingController(sync, () => {}, () => {});
    const g = ctx();
    c.setSymbol("BITUNIX:BTCUSDT");
    expect(sync.ops).toEqual(["load BTCUSDT"]);
    c.setTool("trend");
    c.pointerDown(g, 100, 100, ev());
    c.pointerUp(g, 100, 100);
    c.pointerMove(g, 300, 200, ev({ buttons: 0 }));
    c.pointerDown(g, 300, 200, ev());
    c.pointerUp(g, 300, 200);
    expect(c.items).toHaveLength(1);
    expect(c.items[0]!.pts).toEqual([{ t: T0 + 100_000, p: 115 }, { t: T0 + 300_000, p: 105 }]);
    expect(sync.ops.at(-1)).toBe("put BTCUSDT trend");
    expect(c.tool).toBeNull(); // ohne "Weiterzeichnen" zurück zum Cursor

    // Linie greifen und um 50 px nach rechts ziehen
    expect(c.pointerDown(g, 200, 150, ev())).toBe(true);
    c.pointerMove(g, 250, 150, ev());
    c.pointerUp(g, 250, 150);
    expect(c.items[0]!.pts[0]!.t).toBe(T0 + 150_000);

    c.undoStep();
    expect(c.items[0]!.pts[0]!.t).toBe(T0 + 100_000);
    c.undoStep();
    expect(c.items).toHaveLength(0);
    expect(sync.ops.at(-1)).toMatch(/^remove BTCUSDT /);
    c.redoStep();
    expect(c.items).toHaveLength(1);
  });

  it("Klick ins Leere gibt das Ereignis an den Chart weiter (verschieben)", () => {
    const c = new DrawingController(new FakeSync(), () => {}, () => {});
    c.setSymbol("BTCUSDT");
    expect(c.pointerDown(ctx(), 500, 250, ev())).toBe(false);
  });

  it("Server-Liste (z. B. von Claude) ersetzt die Zeichnungen des passenden Coins", () => {
    const c = new DrawingController(new FakeSync(), () => {}, () => {});
    c.setSymbol("BTCUSDT");
    const d = { ...drawing("hline", [{ t: T0, p: 100 }]), by: "claude" as const };
    c.onServer("ETHUSDT", [d]);
    expect(c.items).toHaveLength(0);
    c.onServer("BTCUSDT", [d]);
    expect(c.items[0]!.by).toBe("claude");
  });

  it("Magnet rastet am Kerzen-Hoch ein", () => {
    const c = new DrawingController(new FakeSync(), () => {}, () => {});
    c.setSymbol("BTCUSDT");
    c.magnet = "strong";
    c.setTool("hline");
    const g = ctx();
    // Kerze 2 (T0+2 min): Hoch 101.2 -> y = 238; Klick knapp daneben
    c.pointerDown(g, 125, 240, ev());
    expect(c.items[0]!.pts[0]!.p).toBeCloseTo(101.2);
  });

  it("Entf löscht die Auswahl, Esc bricht ein Werkzeug ab", () => {
    const c = new DrawingController(new FakeSync(), () => {}, () => {});
    c.setSymbol("BTCUSDT");
    c.onServer("BTCUSDT", [drawing("hline", [{ t: T0, p: 100 }])]);
    c.pointerDown(ctx(), 400, 250, ev());
    expect(c.selected).toBe("x-hline");
    expect(c.key_(new KeyboardEventLike("Delete") as unknown as KeyboardEvent)).toBe(true);
    expect(c.items).toHaveLength(0);
    c.setTool("rect");
    expect(c.key_(new KeyboardEventLike("Escape") as unknown as KeyboardEvent)).toBe(true);
    expect(c.tool).toBeNull();
  });
});

class KeyboardEventLike {
  ctrlKey = false;
  metaKey = false;
  shiftKey = false;
  constructor(public key: string) {}
}
