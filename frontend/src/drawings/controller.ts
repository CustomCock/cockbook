/**
 * Zeichen-Steuerung: Werkzeug wählen, Punkte setzen, auswählen, verschieben, löschen,
 * Rückgängig/Wiederholen, Magnet, Messen, Zoom-Rechteck, Cursor-Effekte.
 *
 * Die Zeichnungen selbst liegen im Backend (je Coin). Jede Änderung geht als einzelne
 * Operation dorthin (put/delete) und kommt als Liste zurück – so landen auch Zeichnungen,
 * die Claude über die Erweiterung setzt, sofort im Chart.
 */

import { build, hitPrims, type Ctx, type Prim, type XY } from "./geom";
import { drawPrims, drawHandles } from "./render";
import { drawingKey, newId, styleFor, TOOL_BY_ID, type Drawing, type Pt, type Style } from "./model";

export type CursorMode = "cross" | "dot" | "arrow" | "demo" | "magic" | "eraser";
export type Magnet = "off" | "weak" | "strong";

export interface DrawingSync {
  put(key: string, d: Drawing): void;
  remove(key: string, id: string): void;
  clear(key: string): void;
  load(key: string): void;
}

interface Trail {
  x: number;
  y: number;
  at: number;
}

const HANDLE_PX = 7;
const UNDO_MAX = 100;

export class DrawingController {
  /** Zeichnungen des aktuellen Coins (Reihenfolge = Zeichenreihenfolge) */
  items: Drawing[] = [];
  key: string | null = null;
  tool: string | null = null;
  cursor: CursorMode = "cross";
  magnet: Magnet = "off";
  keepDrawing = false;
  lockAll = false;
  hideDrawings = false;
  hideTrading = false;
  measureMode = false;
  zoomMode = false;
  selected: string | null = null;
  version = 0;
  /** Vorgegebener Text fürs nächste Text-Werkzeug (z. B. Emoji aus dem Icon-Menü) – keine Rückfrage */
  presetText: string | null = null;

  private draft: Drawing | null = null;
  private draftFixed = 0; // bereits gesetzte Punkte des Entwurfs
  private downAt: XY | null = null;
  private drag: { id: string; handle: number | null; start: Pt; orig: Pt[] } | null = null;
  private measure: { a: Pt; b: Pt; done: boolean } | null = null;
  private zoomRect: { a: XY; b: XY } | null = null;
  private trail: Trail[] = [];
  private undo: Drawing[][] = [];
  private redo: Drawing[][] = [];
  private clipboard: Drawing | null = null;
  private cache = new Map<string, Prim[]>();

  constructor(
    private sync: DrawingSync,
    private onZoom: (t0: number, t1: number, p0: number, p1: number) => void,
    private onChange: () => void,
  ) {}

  // --- Daten ---------------------------------------------------------------------------

  setSymbol(symbol: string | null): void {
    const key = symbol ? drawingKey(symbol) : null;
    if (key === this.key) return;
    this.key = key;
    this.items = [];
    this.selected = null;
    this.draft = null;
    this.undo = [];
    this.redo = [];
    if (key) this.sync.load(key);
    this.changed();
  }

  /** Liste vom Backend (nach eigener oder fremder Änderung, z. B. durch Claude). */
  onServer(key: string, items: Drawing[]): void {
    if (key !== this.key) return;
    if (this.drag) {
      // Während des Ziehens nicht überschreiben – die eigene Änderung kommt gleich zurück
      const dragged = this.items.find((d) => d.id === this.drag!.id);
      this.items = items.map((d) => (dragged && d.id === dragged.id ? dragged : d));
    } else this.items = items;
    if (this.selected && !this.items.some((d) => d.id === this.selected)) this.selected = null;
    this.changed();
  }

  get selectedDrawing(): Drawing | null {
    return this.items.find((d) => d.id === this.selected) ?? null;
  }

  private changed(): void {
    this.version++;
    this.cache.clear();
    this.onChange();
  }

  private snapshot(): void {
    this.undo.push(this.items.map((d) => structuredClone(d)));
    if (this.undo.length > UNDO_MAX) this.undo.shift();
    this.redo = [];
  }

  /** Neue Liste übernehmen und die Unterschiede ans Backend schicken. */
  private apply(next: Drawing[]): void {
    if (!this.key) return;
    const before = new Map(this.items.map((d) => [d.id, d]));
    const after = new Map(next.map((d) => [d.id, d]));
    for (const [id] of before) if (!after.has(id)) this.sync.remove(this.key, id);
    for (const [id, d] of after) if (JSON.stringify(before.get(id)) !== JSON.stringify(d)) this.sync.put(this.key, d);
    this.items = next;
    this.changed();
  }

  update(id: string, fn: (d: Drawing) => void): void {
    const d = this.items.find((x) => x.id === id);
    if (!d) return;
    this.snapshot();
    const copy = structuredClone(d);
    fn(copy);
    this.apply(this.items.map((x) => (x.id === id ? copy : x)));
  }

  setStyle(id: string, patch: Partial<Style>): void {
    this.update(id, (d) => Object.assign(d.style, patch));
  }

  remove(id: string): void {
    this.snapshot();
    this.apply(this.items.filter((d) => d.id !== id));
    if (this.selected === id) this.selected = null;
  }

  removeAll(): void {
    if (!this.key || !this.items.length) return;
    this.snapshot();
    this.items = [];
    this.selected = null;
    this.sync.clear(this.key);
    this.changed();
  }

  clone(id: string): void {
    const d = this.items.find((x) => x.id === id);
    if (!d) return;
    this.snapshot();
    const c = structuredClone(d);
    c.id = newId();
    c.created = Date.now();
    c.by = "user";
    this.apply([...this.items, c]);
    this.selected = c.id;
  }

  undoStep(): void {
    const prev = this.undo.pop();
    if (!prev) return;
    this.redo.push(this.items.map((d) => structuredClone(d)));
    this.apply(prev);
  }

  redoStep(): void {
    const next = this.redo.pop();
    if (!next) return;
    this.undo.push(this.items.map((d) => structuredClone(d)));
    this.apply(next);
  }

  get canUndo(): boolean {
    return this.undo.length > 0;
  }

  get canRedo(): boolean {
    return this.redo.length > 0;
  }

  // --- Werkzeuge -----------------------------------------------------------------------

  setTool(tool: string | null, presetText: string | null = null): void {
    this.tool = tool;
    this.presetText = presetText;
    this.measureMode = false;
    this.zoomMode = false;
    this.draft = null;
    this.changed();
  }

  setCursor(c: CursorMode): void {
    this.cursor = c;
    this.setTool(null);
  }

  get busy(): boolean {
    return !!this.tool || this.measureMode || this.zoomMode || this.cursor === "eraser" || !!this.draft;
  }

  /** CSS-Cursor über dem Chart */
  cssCursor(hover: boolean): string {
    if (this.tool || this.measureMode || this.zoomMode) return "crosshair";
    if (this.cursor === "eraser") return hover ? "not-allowed" : "default";
    if (this.cursor === "arrow" || this.cursor === "demo" || this.cursor === "magic") return hover ? "pointer" : "default";
    return hover ? "pointer" : "crosshair";
  }

  /** Effekt-Cursor brauchen laufend neue Frames, solange die Spur verblasst. */
  get animating(): boolean {
    return this.trail.length > 0;
  }

  // --- Eingabe -------------------------------------------------------------------------

  private pt(ctx: Ctx, x: number, y: number, shift = false): Pt {
    let t = ctx.toT(x);
    let p = ctx.toP(y);
    if (this.magnet !== "off" && !shift) {
      const bar = nearestBar(ctx, t);
      if (bar) {
        const cands = [bar.o, bar.h, bar.l, bar.c];
        let best = cands[0]!;
        for (const c of cands) if (Math.abs(ctx.toY(c) - y) < Math.abs(ctx.toY(best) - y)) best = c;
        const close = Math.abs(ctx.toY(best) - y) <= 14;
        if (this.magnet === "strong" || close) {
          p = best;
          t = bar.t + 30_000;
        }
      }
    }
    return { t, p };
  }

  private hit(ctx: Ctx, x: number, y: number): { d: Drawing; handle: number | null } | null {
    if (this.hideDrawings) return null;
    for (let i = this.items.length - 1; i >= 0; i--) {
      const d = this.items[i]!;
      if (d.hidden) continue;
      if (d.id === this.selected) {
        const h = d.pts.findIndex((q) => Math.hypot(ctx.toX(q.t) - x, ctx.toY(q.p) - y) <= HANDLE_PX);
        if (h >= 0) return { d, handle: h };
      }
      if (hitPrims(this.prims(d, ctx), x, y)) return { d, handle: null };
    }
    return null;
  }

  hoverHit(ctx: Ctx, x: number, y: number): boolean {
    return !!this.hit(ctx, x, y);
  }

  /** Rückgabe true = Ereignis verbraucht (kein Chart-Verschieben). */
  pointerDown(ctx: Ctx, x: number, y: number, e: PointerEvent | MouseEvent): boolean {
    this.downAt = [x, y];
    if (this.cursor === "demo" || this.cursor === "magic") this.trail.push({ x, y, at: performance.now() });
    if (this.zoomMode) {
      this.zoomRect = { a: [x, y], b: [x, y] };
      return true;
    }
    if (this.measureMode || (e.shiftKey && !this.tool)) {
      const p = this.pt(ctx, x, y, true);
      if (this.measure && !this.measure.done) this.measure.done = true;
      else this.measure = { a: p, b: p, done: false };
      this.measureMode = true;
      this.changed();
      return true;
    }
    if (this.measure) {
      this.measure = null;
      this.changed();
    }
    if (this.tool) {
      this.addPoint(ctx, x, y, e.shiftKey);
      return true;
    }
    const h = this.hit(ctx, x, y);
    if (this.cursor === "eraser") {
      if (h) this.remove(h.d.id);
      return true;
    }
    if (h) {
      this.selected = h.d.id;
      if (!h.d.locked && !this.lockAll) {
        this.snapshot();
        this.drag = { id: h.d.id, handle: h.handle, start: this.pt(ctx, x, y, true), orig: h.d.pts.map((q) => ({ ...q })) };
      }
      this.changed();
      return true;
    }
    if (this.selected) {
      this.selected = null;
      this.changed();
    }
    return false;
  }

  pointerMove(ctx: Ctx, x: number, y: number, e: PointerEvent | MouseEvent): boolean {
    if (this.cursor === "demo" || this.cursor === "magic") {
      if (e.buttons || this.cursor === "magic") this.trail.push({ x, y, at: performance.now() });
      this.version++;
    }
    if (this.zoomRect) {
      this.zoomRect.b = [x, y];
      this.version++;
      return true;
    }
    if (this.measure && !this.measure.done) {
      this.measure.b = this.pt(ctx, x, y, true);
      this.version++;
      return true;
    }
    if (this.draft) {
      const p = this.pt(ctx, x, y, e.shiftKey);
      const spec = TOOL_BY_ID.get(this.draft.tool)!.points;
      if (spec === "free") {
        if (e.buttons) {
          const last = this.draft.pts[this.draft.pts.length - 1]!;
          if (Math.hypot(ctx.toX(last.t) - x, ctx.toY(last.p) - y) > 3) this.draft.pts.push(p);
        }
      } else {
        this.draft.pts[this.draftFixed] = p;
        this.draft.pts.length = this.draftFixed + 1;
      }
      this.cache.delete(this.draft.id);
      this.version++;
      return true;
    }
    if (this.drag) {
      const d = this.items.find((q) => q.id === this.drag!.id);
      if (!d) return true;
      const now = this.pt(ctx, x, y, e.shiftKey);
      if (this.drag.handle !== null) {
        d.pts[this.drag.handle] = now;
      } else {
        const dt = now.t - this.drag.start.t;
        const dp = now.p - this.drag.start.p;
        d.pts = this.drag.orig.map((q) => ({ t: q.t + dt, p: q.p + dp }));
      }
      this.cache.delete(d.id);
      this.version++;
      return true;
    }
    return false;
  }

  pointerUp(ctx: Ctx, x: number, y: number): boolean {
    if (this.zoomRect) {
      const { a, b } = this.zoomRect;
      this.zoomRect = null;
      if (Math.abs(a[0] - b[0]) > 8 && Math.abs(a[1] - b[1]) > 8) {
        this.onZoom(ctx.toT(Math.min(a[0], b[0])), ctx.toT(Math.max(a[0], b[0])), ctx.toP(Math.max(a[1], b[1])), ctx.toP(Math.min(a[1], b[1])));
      }
      this.zoomMode = false;
      this.changed();
      return true;
    }
    if (this.measure && !this.measure.done && this.downAt && Math.hypot(this.downAt[0] - x, this.downAt[1] - y) > 4) {
      this.measure.done = true; // gezogen statt geklickt -> fertig
      this.measureMode = false;
      this.changed();
      return true;
    }
    if (this.draft) {
      const spec = TOOL_BY_ID.get(this.draft.tool)!.points;
      const moved = this.downAt && Math.hypot(this.downAt[0] - x, this.downAt[1] - y) > 4;
      if (spec === "free") this.finish();
      else if (spec === 2 && moved && this.draftFixed === 1) this.finish(); // gezogen statt zweimal geklickt
      return true;
    }
    if (this.drag) {
      const d = this.items.find((q) => q.id === this.drag!.id);
      this.drag = null;
      if (d) this.sync.put(this.key!, d);
      this.changed();
      return true;
    }
    return false;
  }

  doubleClick(): boolean {
    if (this.draft && TOOL_BY_ID.get(this.draft.tool)!.points === "multi") {
      // Der zweite Klick des Doppelklicks hat schon einen Punkt gesetzt -> wieder weg
      this.draft.pts.length = Math.max(2, this.draftFixed);
      this.finish();
      return true;
    }
    const d = this.selectedDrawing;
    if (d && TOOL_BY_ID.get(d.tool)?.askText) {
      this.editText(d.id);
      return true;
    }
    return !!this.draft;
  }

  key_(e: KeyboardEvent): boolean {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "z") {
      if (e.shiftKey) this.redoStep();
      else this.undoStep();
      return true;
    }
    if (mod && e.key.toLowerCase() === "y") {
      this.redoStep();
      return true;
    }
    if (mod && e.key.toLowerCase() === "c" && this.selectedDrawing) {
      this.clipboard = structuredClone(this.selectedDrawing);
      return true;
    }
    if (mod && e.key.toLowerCase() === "v" && this.clipboard) {
      const c = structuredClone(this.clipboard);
      c.id = newId();
      c.created = Date.now();
      // leicht versetzt einfügen, damit man die Kopie sieht
      const span = Math.abs((c.pts[1]?.t ?? c.pts[0]!.t + 600_000) - c.pts[0]!.t) * 0.1 || 60_000;
      c.pts = c.pts.map((q) => ({ t: q.t + span, p: q.p }));
      this.snapshot();
      this.apply([...this.items, c]);
      this.selected = c.id;
      return true;
    }
    if (e.key === "Escape") {
      if (this.draft || this.tool || this.measure || this.zoomMode || this.selected) {
        this.draft = null;
        this.measure = null;
        this.measureMode = false;
        this.zoomMode = false;
        this.zoomRect = null;
        if (!this.keepDrawing) this.tool = null;
        this.selected = null;
        this.changed();
        return true;
      }
      return false;
    }
    if (e.key === "Enter" && this.draft && TOOL_BY_ID.get(this.draft.tool)!.points === "multi") {
      this.draft.pts.length = Math.max(2, this.draftFixed);
      this.finish();
      return true;
    }
    if ((e.key === "Delete" || e.key === "Backspace") && this.selected) {
      this.remove(this.selected);
      return true;
    }
    return false;
  }

  private addPoint(ctx: Ctx, x: number, y: number, shift: boolean): void {
    const def = TOOL_BY_ID.get(this.tool!);
    if (!def) return;
    const p = this.pt(ctx, x, y, shift);
    if (!this.draft) {
      this.draft = { id: newId(), tool: def.id as Drawing["tool"], pts: [p, { ...p }], style: styleFor(def.id), created: Date.now(), by: "user" };
      this.draftFixed = 1;
      if (def.points === 1) {
        this.draft.pts.length = 1;
        this.expandSinglePoint(ctx, this.draft);
        this.finish();
      } else if (def.points === "free") this.draft.pts.length = 1;
      this.changed();
      return;
    }
    // weiterer Punkt
    this.draft.pts[this.draftFixed] = p;
    this.draftFixed++;
    if (typeof def.points === "number" && this.draftFixed >= def.points) {
      this.draft.pts.length = def.points;
      this.finish();
    } else this.draft.pts.push({ ...p });
    this.changed();
  }

  /** Werkzeuge mit einem Klick, die intern mehr Punkte brauchen (Long/Short, Gann fest). */
  private expandSinglePoint(ctx: Ctx, d: Drawing): void {
    const e = d.pts[0]!;
    // Größe relativ zur Ansicht (feste ±% wären je nach Zoom unsichtbar klein oder riesig):
    // 160 px breit, Ziel 80 px, Stop 40 px entfernt -> CRV 2
    const x = ctx.toX(e.t);
    const y = ctx.toY(e.p);
    const end = ctx.toT(x + 160);
    if (d.tool === "long") d.pts = [e, { t: end, p: ctx.toP(y - 80) }, { t: end, p: ctx.toP(y + 40) }];
    if (d.tool === "short") d.pts = [e, { t: end, p: ctx.toP(y + 80) }, { t: end, p: ctx.toP(y - 40) }];
    if (d.tool === "gannsqfixed") d.pts = [e, { t: ctx.toT(ctx.toX(e.t) + 160), p: ctx.toP(ctx.toY(e.p) - 160) }];
  }

  private finish(): void {
    const d = this.draft;
    if (!d) return;
    this.draft = null;
    const def = TOOL_BY_ID.get(d.tool)!;
    if (def.askText && this.presetText !== null) {
      d.style.text = this.presetText;
    } else if (def.askText) {
      const t = prompt(def.id === "table" ? "Tabelle: Spalten mit | trennen, Zeilen mit Enter (\\n)" : `${def.label}: Text`, d.style.text);
      if (t === null) {
        if (!this.keepDrawing) this.tool = null;
        this.changed();
        return;
      }
      d.style.text = def.id === "table" ? t.replace(/\\n/g, "\n") : t;
    }
    this.snapshot();
    this.apply([...this.items, d]);
    this.selected = d.id;
    if (!this.keepDrawing) this.tool = null;
  }

  editText(id: string): void {
    const d = this.items.find((x) => x.id === id);
    if (!d) return;
    const t = prompt("Text", d.style.text);
    if (t !== null) this.setStyle(id, { text: d.tool === "table" ? t.replace(/\\n/g, "\n") : t });
  }

  // --- Zeichnen ------------------------------------------------------------------------

  private prims(d: Drawing, ctx: Ctx): Prim[] {
    // Zwischenspeicher je Zeichnung – gilt nur für unveränderte Sicht (wird pro Frame geleert)
    let p = this.cache.get(d.id);
    if (!p) {
      p = build(d, ctx);
      this.cache.set(d.id, p);
    }
    return p;
  }

  /** Vor jedem Frame: Sicht hat sich evtl. geändert -> Grundformen neu berechnen. */
  invalidate(): void {
    this.cache.clear();
  }

  draw(canvas: HTMLCanvasElement, dpr: number, ctx: Ctx): void {
    const g = canvas.getContext("2d")!;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, ctx.pw, ctx.ph);
    this.cache.clear();
    if (!this.hideDrawings) {
      for (const d of this.items) {
        if (d.hidden) continue;
        const sel = d.id === this.selected;
        drawPrims(g, this.prims(d, ctx), sel);
        if (sel) drawHandles(g, d.pts.map((q) => [ctx.toX(q.t), ctx.toY(q.p)]), d.style.color);
      }
    }
    if (this.draft) {
      drawPrims(g, build(this.draft, ctx), true);
      drawHandles(g, this.draft.pts.map((q) => [ctx.toX(q.t), ctx.toY(q.p)]), this.draft.style.color);
    }
    if (this.measure) {
      const m: Drawing = { id: "measure", tool: "dateprice", pts: [this.measure.a, this.measure.b], style: styleFor("dateprice", { color: "#dfe1e4" }), created: 0 };
      drawPrims(g, build(m, ctx), false);
    }
    if (this.zoomRect) {
      const { a, b } = this.zoomRect;
      g.strokeStyle = "#b99cff";
      g.setLineDash([4, 3]);
      g.strokeRect(Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.abs(a[0] - b[0]), Math.abs(a[1] - b[1]));
      g.fillStyle = "rgba(185,156,255,0.08)";
      g.fillRect(Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.abs(a[0] - b[0]), Math.abs(a[1] - b[1]));
      g.setLineDash([]);
    }
    this.drawTrail(g);
  }

  private drawTrail(g: CanvasRenderingContext2D): void {
    const now = performance.now();
    const life = this.cursor === "magic" ? 700 : 1200;
    this.trail = this.trail.filter((p) => now - p.at < life).slice(-400);
    if (!this.trail.length) return;
    if (this.cursor === "demo") {
      // Laserpointer-Spur für Erklärungen/Videos
      g.lineCap = "round";
      for (let i = 1; i < this.trail.length; i++) {
        const a = this.trail[i - 1]!;
        const b = this.trail[i]!;
        g.globalAlpha = Math.max(0, 1 - (now - b.at) / life);
        g.strokeStyle = "#ff4d6d";
        g.lineWidth = 4;
        g.beginPath();
        g.moveTo(a.x, a.y);
        g.lineTo(b.x, b.y);
        g.stroke();
      }
    } else {
      for (const p of this.trail) {
        const f = 1 - (now - p.at) / life;
        g.globalAlpha = Math.max(0, f);
        g.fillStyle = ["#ffe24f", "#b99cff", "#5accff"][Math.floor(p.at) % 3]!;
        const r = 1 + f * 3;
        const jx = Math.sin(p.at * 0.37) * (1 - f) * 18;
        const jy = Math.cos(p.at * 0.53) * (1 - f) * 18;
        g.beginPath();
        g.arc(p.x + jx, p.y + jy, r, 0, Math.PI * 2);
        g.fill();
      }
    }
    g.globalAlpha = 1;
  }
}

function nearestBar(ctx: Ctx, t: number) {
  const bars = ctx.bars;
  let lo = 0;
  let hi = bars.length - 1;
  if (hi < 0) return null;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (bars[mid]!.t + 60_000 <= t) lo = mid + 1;
    else hi = mid;
  }
  const b = bars[lo]!;
  return t >= b.t - 60_000 && t <= b.t + 120_000 ? b : null;
}
