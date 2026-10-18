/** Grundformen (siehe geom.ts) auf ein Canvas2D zeichnen. */

import type { Prim, XY } from "./geom";

const DASH: Record<string, number[]> = { solid: [], dashed: [7, 5], dotted: [2, 4] };

export function drawPrims(ctx: CanvasRenderingContext2D, prims: Prim[], selected: boolean): void {
  for (const p of prims) {
    ctx.globalAlpha = p.alpha ?? 1;
    if (p.kind === "text") {
      drawText(ctx, p);
      continue;
    }
    const pts = p.pts ?? [];
    if (!pts.length) continue;
    ctx.beginPath();
    ctx.moveTo(pts[0]![0], pts[0]![1]);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i]![0], pts[i]![1]);
    if (p.kind === "poly" && p.closed) ctx.closePath();
    if (p.kind === "poly" && p.fill) {
      ctx.fillStyle = p.fill;
      ctx.fill();
    }
    if (p.color) {
      ctx.strokeStyle = p.color;
      ctx.lineWidth = (p.width ?? 1) + (selected ? 0.5 : 0);
      ctx.setLineDash(DASH[p.dash ?? "solid"]!);
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;
  ctx.setLineDash([]);
}

function drawText(ctx: CanvasRenderingContext2D, p: Prim): void {
  const size = p.size ?? 12;
  ctx.font = `${size}px ui-monospace, monospace`;
  ctx.textAlign = p.align ?? "left";
  ctx.textBaseline = "middle";
  const lines = (p.text ?? "").split("\n");
  const lh = size * 1.3;
  const y0 = p.y! - ((lines.length - 1) * lh) / 2;
  if (p.bg) {
    const w = Math.max(...lines.map((l) => ctx.measureText(l).width));
    const x0 = p.align === "right" ? p.x! - w : p.align === "center" ? p.x! - w / 2 : p.x!;
    ctx.fillStyle = p.bg;
    roundRect(ctx, x0 - 5, y0 - lh / 2 - 2, w + 10, lines.length * lh + 4, 4);
    ctx.fill();
  }
  if (!p.bg) {
    // Dunkler Rand: Text bleibt auf der bunten Heatmap lesbar
    ctx.strokeStyle = "rgba(20,21,24,0.85)";
    ctx.lineWidth = 3;
    ctx.lineJoin = "round";
    ctx.setLineDash([]);
    lines.forEach((l, i) => ctx.strokeText(l, p.x!, y0 + i * lh));
  }
  ctx.fillStyle = p.color ?? "#dfe1e4";
  lines.forEach((l, i) => ctx.fillText(l, p.x!, y0 + i * lh));
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/** Anfasser an den Ankerpunkten einer ausgewählten Zeichnung. */
export function drawHandles(ctx: CanvasRenderingContext2D, pts: XY[], color: string): void {
  for (const [x, y] of pts) {
    ctx.beginPath();
    ctx.arc(x, y, 5, 0, Math.PI * 2);
    ctx.fillStyle = "#1d1e21";
    ctx.fill();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = color;
    ctx.stroke();
  }
}
