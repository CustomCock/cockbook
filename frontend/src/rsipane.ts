/**
 * RSI-Fenster unter dem Chart (Canvas2D). Eine Linie mit ein paar hundert Punkten ist
 * für Canvas2D billig; gezeichnet wird nur, wenn sich etwas ändert (wie der Plot).
 * Die x-Achse ist dieselbe wie bei Heatmap und Kerzen (`timeToX`).
 */

import { AXIS_W } from "./axes";
import { timeToX, type Bar, type CandleScene } from "./candles";
import { RSI_OVERBOUGHT, RSI_OVERSOLD, RSI_PERIOD, rsiSeries, rsiZone } from "./rsi";

export const RSI_PANE_H = 110;

const BG = "#232427";
const AXIS_BG = "#222326";
const BORDER = "#34363b";
const TEXT = "#a9adb3";
const LINE = "#b49cff";
const ZONE = "rgba(180, 156, 255, 0.07)";
const LEVEL = "rgba(200, 205, 212, 0.28)";
const ZONE_COLOR = { overbought: "#ff7385", oversold: "#55e5a8", neutral: LINE } as const;

/**
 * `bars`: Kerzen im gewählten Intervall (sortiert), `scene`: Zeitachse des Plots oder
 * null (noch keine Heatmap-Daten).
 */
export function drawRsiPane(
  canvas: HTMLCanvasElement,
  dpr: number,
  bars: Bar[],
  minutes: number,
  scene: CandleScene | null,
  pw: number,
): number | null {
  const ctx = canvas.getContext("2d")!;
  const h = canvas.height / dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = BG;
  ctx.fillRect(0, 0, pw, h);
  ctx.fillStyle = AXIS_BG;
  ctx.fillRect(pw, 0, AXIS_W, h);
  ctx.fillStyle = BORDER;
  ctx.fillRect(0, 0, pw + AXIS_W, 1);

  const top = 8;
  const bottom = h - 6;
  const yOf = (v: number) => bottom - (v / 100) * (bottom - top);
  ctx.fillStyle = ZONE;
  ctx.fillRect(0, yOf(RSI_OVERBOUGHT), pw, yOf(RSI_OVERSOLD) - yOf(RSI_OVERBOUGHT));
  ctx.strokeStyle = LEVEL;
  ctx.lineWidth = 1;
  for (const [lvl, dash] of [
    [RSI_OVERBOUGHT, [4, 4]],
    [50, [1, 4]],
    [RSI_OVERSOLD, [4, 4]],
  ] as const) {
    ctx.setLineDash(dash as unknown as number[]);
    ctx.beginPath();
    ctx.moveTo(0, Math.round(yOf(lvl)) + 0.5);
    ctx.lineTo(pw, Math.round(yOf(lvl)) + 0.5);
    ctx.stroke();
  }
  ctx.setLineDash([]);
  ctx.font = "11px ui-monospace, monospace";
  ctx.textBaseline = "middle";
  ctx.fillStyle = TEXT;
  const rsi = rsiSeries(bars.map((b) => b.c), RSI_PERIOD);
  const last = rsi.length ? rsi[rsi.length - 1]! : NaN;
  const lastVal = Number.isNaN(last) ? null : last;
  const badge = lastVal !== null && scene && scene.view.offset <= 0 ? yOf(lastVal) : null;
  for (const lvl of [RSI_OVERBOUGHT, 50, RSI_OVERSOLD]) {
    // Nicht unter der Marke des aktuellen Werts verstecken
    if (badge === null || Math.abs(yOf(lvl) - badge) > 12) ctx.fillText(String(lvl), pw + 6, yOf(lvl));
  }
  ctx.textBaseline = "top";
  ctx.fillStyle = TEXT;
  const title = `RSI ${RSI_PERIOD} · ${minutes}m`;
  ctx.fillText(title, 8, 6);
  if (lastVal !== null) {
    ctx.fillStyle = ZONE_COLOR[rsiZone(lastVal)];
    ctx.fillText(lastVal.toFixed(1), 8 + ctx.measureText(title).width + 10, 6);
  }
  if (!scene || scene.store.count === 0) return lastVal;

  // Linie: ein Punkt je Kerze in der Kerzenmitte
  const p = minutes * 60_000;
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, pw, h);
  ctx.clip();
  ctx.strokeStyle = LINE;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = "round";
  ctx.beginPath();
  let started = false;
  for (let i = 0; i < bars.length; i++) {
    const v = rsi[i]!;
    if (Number.isNaN(v)) continue;
    const b = bars[i]!;
    const x = (timeToX(b.t, scene) + timeToX(b.t + p, scene)) / 2;
    if (x < -50) continue;
    const y = yOf(v);
    if (started) ctx.lineTo(x, y);
    else ctx.moveTo(x, y);
    started = true;
    if (x > pw + 50) break;
  }
  ctx.stroke();
  ctx.restore();

  if (lastVal !== null && scene.view.offset <= 0) {
    const y = yOf(lastVal);
    ctx.fillStyle = ZONE_COLOR[rsiZone(lastVal)];
    ctx.fillRect(pw + 1, y - 8, AXIS_W - 2, 16);
    ctx.fillStyle = "#1d2026";
    ctx.textBaseline = "middle";
    ctx.fillText(lastVal.toFixed(1), pw + 6, y);
  }
  return lastVal;
}
