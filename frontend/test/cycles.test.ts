import { describe, expect, it } from "vitest";
import { athStats, cycleStats, DAY_MS, parseDate, type DailyRow } from "../src/cycles";

/** Tageskerzen ab `start` mit gegebenen Schlusskursen (open = vorheriger close). */
function rows(start: number, closes: number[]): DailyRow[] {
  return closes.map((c, i) => {
    const o = i ? closes[i - 1]! : c;
    return [start + i * DAY_MS, o, Math.max(o, c), Math.min(o, c), c, 1];
  });
}

describe("athStats", () => {
  const jan1 = parseDate("2026-01-01");
  const data = rows(parseDate("2025-12-30"), [100, 110, 120, 200, 150]); // 30.12. .. 03.01.

  it("ATH, Abstand und Tage seit ATH", () => {
    const now = jan1 + 2 * DAY_MS + 3600_000; // 03.01. 01:00
    const s = athStats(data, 150, now)!;
    expect(s.ath).toBe(200);
    expect(s.athTs).toBe(jan1 + DAY_MS);
    expect(s.fromAthPct).toBeCloseTo(-25);
    expect(s.daysSinceAth).toBe(1);
    expect(s.atl).toBe(100);
  });

  it("YTD gegen Eröffnung am 1. Januar", () => {
    const s = athStats(data, 150, jan1 + 3 * DAY_MS)!;
    expect(s.yearOpen).toBe(110); // Kerze 01.01. öffnet mit Schluss vom 31.12.
    expect(s.ytdPct).toBeCloseTo((150 / 110 - 1) * 100);
  });

  it("YTD unbekannt, wenn Daten erst im Jahr beginnen", () => {
    const s = athStats(rows(jan1 + 5 * DAY_MS, [1, 2]), 2, jan1 + 10 * DAY_MS)!;
    expect(s.ytdPct).toBeNull();
  });

  it("Live-Preis über dem Tages-Hoch ist sofort neues ATH", () => {
    const now = jan1 + 3 * DAY_MS;
    const s = athStats(data, 250, now)!;
    expect(s.ath).toBe(250);
    expect(s.daysSinceAth).toBe(0);
  });
});

describe("cycleStats", () => {
  const start = parseDate("2020-05-11");
  const end = start + 10 * DAY_MS;
  // Tag 1..10: Hoch am Tag 4 (close 300), Ende bei 150
  const data = rows(start, [100, 150, 200, 300, 250, 220, 180, 160, 155, 150, 999]);

  it("abgeschlossener Zyklus: Länge, Hoch, Tief, Performance", () => {
    const c = cycleStats(data, start, end, end + 100 * DAY_MS, 999)!;
    expect(c.complete).toBe(true);
    expect(c.lengthDays).toBe(10);
    expect(c.high).toBe(300);
    expect(c.highDay).toBe(4);
    expect(c.startPrice).toBe(100);
    expect(c.endPrice).toBe(150); // Schluss am letzten Zyklustag, NICHT Live-Preis
    expect(c.startToHighPct).toBeCloseTo(200);
    expect(c.highToEndPct).toBeCloseTo(-50);
    expect(c.startToEndPct).toBeCloseTo(50);
    expect(c.partial).toBe(false);
  });

  it("laufender Zyklus: Fortschritt und Live-Preis", () => {
    const now = start + 5 * DAY_MS + 1000;
    const c = cycleStats(data, start, end, now, 260)!;
    expect(c.complete).toBe(false);
    expect(c.elapsedDays).toBe(5);
    expect(c.progressPct).toBeCloseTo(50);
    expect(c.endPrice).toBe(260);
    expect(c.highToEndPct).toBeCloseTo((260 / 300 - 1) * 100);
  });

  it("Daten beginnen nach Zyklusstart -> als unvollständig markiert", () => {
    const c = cycleStats(data, start - 30 * DAY_MS, end, end + DAY_MS, null)!;
    expect(c.partial).toBe(true);
    expect(c.firstDataTs).toBe(start);
  });

  it("kein Zeitraum / keine Daten", () => {
    expect(cycleStats(data, end, start, end, null)).toBeNull();
    expect(cycleStats(data, start + 1000 * DAY_MS, start + 1100 * DAY_MS, start + 1200 * DAY_MS, null)).toBeNull();
  });
});
