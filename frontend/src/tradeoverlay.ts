/**
 * Handels-Linien über der Heatmap: Position (Einstieg, Break-even, Liquidation), TP/SL und
 * offene Orders. Eigenes 2D-Canvas über dem WebGL-Canvas – Text und gestrichelte Linien
 * sind in Canvas2D einfacher, und es sind nur eine Handvoll Linien.
 *
 * Ziehen einer Order- oder TP/SL-Linie verschiebt sie (erst beim Loslassen wird gesendet),
 * × im Etikett löscht die Order bzw. entfernt TP/SL.
 */

import type { TradeLine } from "./paper";

const LABEL_H = 17;
const X_W = 16;
const HIT_PX = 5;

interface Box {
  line: TradeLine;
  y: number;
  /** Oberkante des Etiketts (kann gegenüber der Linie verschoben sein) */
  ly: number;
  x0: number;
  x1: number;
}

export class TradeOverlay {
  private boxes: Box[] = [];
  /** gerade gezogene Linie und ihr vorläufiger Preis */
  dragging: { line: TradeLine; price: number } | null = null;

  constructor(private canvas: HTMLCanvasElement) {}

  draw(dpr: number, pw: number, ph: number, lines: TradeLine[], toY: (price: number) => number, fmt: (price: number) => string): void {
    const c = this.canvas;
    const ctx = c.getContext("2d")!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, pw, ph);
    this.boxes = [];
    ctx.font = "11px ui-monospace, monospace";
    ctx.textBaseline = "middle";
    const drawn = lines.map((l) =>
      this.dragging && sameTarget(this.dragging.line, l) ? { ...l, price: this.dragging.price } : l,
    );
    // Etiketten nicht übereinander: von oben nach unten, notfalls nach unten schieben
    const items = drawn
      .map((l) => ({ l, y: toY(l.price) }))
      .filter((it) => it.y > -20 && it.y < ph + 20)
      .sort((a, b) => a.y - b.y);
    let lastBottom = -Infinity;
    for (const { l, y } of items) {
      const yy = Math.round(y) + 0.5;
      ctx.strokeStyle = l.color;
      ctx.globalAlpha = l.kind === "breakeven" ? 0.6 : 0.9;
      ctx.lineWidth = l.kind === "entry" ? 1.5 : 1;
      ctx.setLineDash(l.kind === "order" || l.kind === "entry" ? [] : l.kind === "liq" ? [2, 3] : [6, 4]);
      ctx.beginPath();
      ctx.moveTo(0, yy);
      ctx.lineTo(pw, yy);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
      // Etikett rechts
      const text = `${l.label} · ${fmt(l.price)}`;
      const tw = ctx.measureText(text).width + 10 + (l.cancel ? X_W : 0);
      let ly = y - LABEL_H / 2;
      if (ly < lastBottom + 1) ly = lastBottom + 1;
      lastBottom = ly + LABEL_H;
      const x1 = pw - 6;
      const x0 = x1 - tw;
      ctx.fillStyle = "rgba(26, 27, 30, 0.88)";
      ctx.fillRect(x0, ly, tw, LABEL_H);
      ctx.fillStyle = l.color;
      ctx.fillRect(x0, ly, 3, LABEL_H);
      ctx.fillText(text, x0 + 7, ly + LABEL_H / 2);
      if (l.cancel) {
        ctx.fillStyle = "#c9ccd1";
        ctx.fillText("×", x1 - X_W + 4, ly + LABEL_H / 2);
      }
      this.boxes.push({ line: l, y, ly, x0, x1 });
    }
  }

  /** Was liegt unter dem Mauszeiger? `cancel` = × im Etikett getroffen. */
  hit(x: number, y: number): { line: TradeLine; cancel: boolean } | null {
    for (const b of this.boxes) {
      const inLabel = x >= b.x0 && x <= b.x1 && y >= b.ly && y <= b.ly + LABEL_H;
      if (inLabel && b.line.cancel && x >= b.x1 - X_W) return { line: b.line, cancel: true };
      if (inLabel || (Math.abs(y - b.y) <= HIT_PX && b.line.drag)) return { line: b.line, cancel: false };
    }
    return null;
  }
}

function sameTarget(a: TradeLine, b: TradeLine): boolean {
  return JSON.stringify(a.drag) === JSON.stringify(b.drag) && a.kind === b.kind;
}
