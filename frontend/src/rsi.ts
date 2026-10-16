/**
 * RSI (Relative Strength Index) nach J. Welles Wilder.
 *
 * 1. Start: einfacher Durchschnitt der Gewinne/Verluste der ersten `period` Änderungen.
 * 2. Danach Wilder-Glättung: avg = (avg * (period - 1) + neu) / period
 *    (entspricht einem EMA mit alpha = 1/period – deshalb "läuft" der RSI ein paar
 *    Dutzend Kerzen ein, bis er mit TradingView & Co. übereinstimmt).
 * 3. RSI = 100 - 100 / (1 + avgGain / avgLoss); ohne Verluste 100, ohne Bewegung 50.
 */

export const RSI_PERIOD = 14;
export const RSI_OVERBOUGHT = 70;
export const RSI_OVERSOLD = 30;

/** RSI je Schlusskurs; NaN, solange noch nicht genug Kerzen da sind (die ersten `period`). */
export function rsiSeries(closes: ArrayLike<number>, period = RSI_PERIOD): Float64Array {
  const n = closes.length;
  const out = new Float64Array(n).fill(NaN);
  if (period < 1 || n <= period) return out;
  let gain = 0;
  let loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = closes[i]! - closes[i - 1]!;
    if (d > 0) gain += d;
    else loss -= d;
  }
  gain /= period;
  loss /= period;
  out[period] = toRsi(gain, loss);
  for (let i = period + 1; i < n; i++) {
    const d = closes[i]! - closes[i - 1]!;
    gain = (gain * (period - 1) + Math.max(d, 0)) / period;
    loss = (loss * (period - 1) + Math.max(-d, 0)) / period;
    out[i] = toRsi(gain, loss);
  }
  return out;
}

function toRsi(gain: number, loss: number): number {
  if (loss === 0) return gain === 0 ? 50 : 100;
  return 100 - 100 / (1 + gain / loss);
}

/** Letzter RSI-Wert oder null (zu wenig Daten). */
export function lastRsi(closes: ArrayLike<number>, period = RSI_PERIOD): number | null {
  const s = rsiSeries(closes, period);
  const v = s[s.length - 1];
  return v === undefined || Number.isNaN(v) ? null : v;
}

export type RsiZone = "overbought" | "oversold" | "neutral";

export function rsiZone(v: number | null): RsiZone {
  if (v === null) return "neutral";
  if (v >= RSI_OVERBOUGHT) return "overbought";
  if (v <= RSI_OVERSOLD) return "oversold";
  return "neutral";
}
