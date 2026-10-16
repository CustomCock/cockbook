/**
 * Allzeithoch, Jahresperformance und Marktzyklen aus Tageskerzen (reine Funktionen).
 *
 * Tageskerze: [Öffnungszeit ms (UTC), open, high, low, close, volume].
 * Ein laufender Preis (Live-Ticker) wird berücksichtigt, damit ein neues ATH sofort
 * zählt und nicht erst mit der nächsten Tageskerze.
 */

export type DailyRow = [number, number, number, number, number, number];

export const DAY_MS = 86_400_000;

export interface AthStats {
  dataStart: number;
  ath: number;
  athTs: number;
  fromAthPct: number;
  daysSinceAth: number;
  atl: number;
  atlTs: number;
  ytdPct: number | null; // null, wenn die Daten nicht bis 1. Januar zurückreichen
  yearOpen: number | null;
  high52w: number;
  low52w: number;
}

export function athStats(rows: DailyRow[], price: number | null, now: number): AthStats | null {
  if (!rows.length) return null;
  const last = rows[rows.length - 1]!;
  const p = price ?? last[4];
  let ath = -Infinity;
  let athTs = 0;
  let atl = Infinity;
  let atlTs = 0;
  let high52 = -Infinity;
  let low52 = Infinity;
  const yearAgo = now - 365 * DAY_MS;
  for (const [t, , h, l] of rows) {
    if (h > ath) [ath, athTs] = [h, t];
    if (l < atl) [atl, atlTs] = [l, t];
    if (t >= yearAgo) {
      high52 = Math.max(high52, h);
      low52 = Math.min(low52, l);
    }
  }
  if (p > ath) [ath, athTs] = [p, now];
  if (p < atl) [atl, atlTs] = [p, now];
  high52 = Math.max(high52, p);
  low52 = Math.min(low52, p);

  const yearStart = Date.UTC(new Date(now).getUTCFullYear(), 0, 1);
  const first = rows[0]![0];
  const yearRow = first <= yearStart ? rows.find((r) => r[0] >= yearStart) : undefined;
  const yearOpen = yearRow ? yearRow[1] : null;
  return {
    dataStart: first,
    ath,
    athTs,
    fromAthPct: (p / ath - 1) * 100,
    daysSinceAth: Math.floor((now - athTs) / DAY_MS),
    atl,
    atlTs,
    ytdPct: yearOpen ? (p / yearOpen - 1) * 100 : null,
    yearOpen,
    high52w: high52,
    low52w: low52,
  };
}

export interface CyclePreset {
  id: string;
  label: string;
  start: string; // YYYY-MM-DD (UTC)
  end: string;
  estimatedEnd?: boolean;
}

/**
 * Bitcoin-Halving-Zyklen – üblicher Takt für den gesamten Kryptomarkt.
 * Halvings: 09.07.2016, 11.05.2020, 20.04.2024; nächstes ≈ April 2028 (geschätzt,
 * hängt von der Blockzeit ab).
 */
export const CYCLE_PRESETS: CyclePreset[] = [
  { id: "c2024", label: "Halving-Zyklus 2024–2028 (aktuell)", start: "2024-04-20", end: "2028-04-15", estimatedEnd: true },
  { id: "c2020", label: "Halving-Zyklus 2020–2024", start: "2020-05-11", end: "2024-04-20" },
  { id: "c2016", label: "Halving-Zyklus 2016–2020", start: "2016-07-09", end: "2020-05-11" },
];

export function parseDate(s: string): number {
  const [y, m, d] = s.split("-").map(Number);
  return Date.UTC(y!, (m ?? 1) - 1, d ?? 1);
}

export interface CycleStats {
  start: number;
  end: number;
  complete: boolean;
  lengthDays: number; // geplante bzw. tatsächliche Länge
  elapsedDays: number;
  progressPct: number;
  /** Daten beginnen erst nach Zyklusstart (z. B. Binance ab 08/2017) */
  partial: boolean;
  firstDataTs: number;
  startPrice: number;
  high: number;
  highTs: number;
  highDay: number; // Tag im Zyklus, an dem das Hoch war
  low: number;
  lowTs: number;
  lowDay: number;
  /** Schlusskurs am Zyklusende bzw. aktueller Preis */
  endPrice: number;
  startToHighPct: number;
  highToEndPct: number; // Abstand vom Zyklus-Hoch (Drawdown)
  startToEndPct: number;
}

export function cycleStats(
  rows: DailyRow[],
  start: number,
  end: number,
  now: number,
  price: number | null,
): CycleStats | null {
  if (end <= start) return null;
  const until = Math.min(end, now);
  const inRange = rows.filter((r) => r[0] >= start && r[0] < until);
  if (!inRange.length) return null;
  const complete = end <= now;
  let high = -Infinity;
  let highTs = 0;
  let low = Infinity;
  let lowTs = 0;
  for (const [t, , h, l] of inRange) {
    if (h > high) [high, highTs] = [h, t];
    if (l < low) [low, lowTs] = [l, t];
  }
  const endPrice = complete ? inRange[inRange.length - 1]![4] : (price ?? inRange[inRange.length - 1]![4]);
  if (!complete && price !== null) {
    if (price > high) [high, highTs] = [price, now];
    if (price < low) [low, lowTs] = [price, now];
  }
  const first = inRange[0]!;
  const startPrice = first[1];
  const lengthDays = Math.round((end - start) / DAY_MS);
  const elapsedDays = Math.min(lengthDays, Math.floor((until - start) / DAY_MS));
  return {
    start,
    end,
    complete,
    lengthDays,
    elapsedDays,
    progressPct: (elapsedDays / lengthDays) * 100,
    partial: first[0] - start >= DAY_MS,
    firstDataTs: first[0],
    startPrice,
    high,
    highTs,
    highDay: Math.floor((highTs - start) / DAY_MS) + 1,
    low,
    lowTs,
    lowDay: Math.floor((lowTs - start) / DAY_MS) + 1,
    endPrice,
    startToHighPct: (high / startPrice - 1) * 100,
    highToEndPct: (endPrice / high - 1) * 100,
    startToEndPct: (endPrice / startPrice - 1) * 100,
  };
}
