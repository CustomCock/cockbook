/**
 * Werkzeugleiste links am Chart: Werkzeuggruppen mit Ausklappmenüs, darunter Hilfsmittel
 * (Messen, Zoom, Magnet, Weiterzeichnen, Sperren, Ausblenden, Entfernen, Objektliste,
 * Rückgängig). Dazu die Stil-Leiste der ausgewählten Zeichnung und die Objektliste.
 */

import type { CursorMode, DrawingController } from "./controller";
import { ICONS } from "./icons";
import { EMOJIS, GROUPS, TOOL_BY_ID, TOOLS, type Dash, type Drawing, type GroupId, type ToolDef } from "./model";

const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

const CURSORS: { id: CursorMode; label: string; icon: string }[] = [
  { id: "cross", label: "Fadenkreuz", icon: "cursor" },
  { id: "dot", label: "Punkt", icon: "dot" },
  { id: "arrow", label: "Pfeil", icon: "arrow" },
  { id: "demo", label: "Präsentation (Laserpointer)", icon: "demo" },
  { id: "magic", label: "Magie (Glitzerspur)", icon: "magic" },
  { id: "eraser", label: "Radierer (Klick löscht Zeichnung)", icon: "eraser" },
];

/** Knopf mit SVG-Symbol */
function iconBtn(icon: string, title: string, cls = "db-btn"): HTMLButtonElement {
  const b = el("button", cls);
  b.type = "button";
  b.title = title;
  b.innerHTML = ICONS[icon] ?? "";
  return b;
}

const COLORS = ["#5accff", "#55e5a8", "#ff7385", "#ffa833", "#b99cff", "#ffe24f", "#dfe1e4", "#787b86"];

export class DrawToolbar {
  readonly root: HTMLElement;
  private groupBtns = new Map<GroupId, HTMLButtonElement>();
  private lastTool = new Map<GroupId, string>();
  private flyout: HTMLElement;
  private utilBtns = new Map<string, HTMLButtonElement>();
  private styleBar: HTMLElement;
  private tree: HTMLElement;
  private treeOpen = false;

  constructor(
    root: HTMLElement,
    styleHost: HTMLElement,
    private c: DrawingController,
    private onHideTrading: () => void,
  ) {
    this.root = root;
    root.classList.add("drawbar");
    for (const g of GROUPS) {
      // Symbol = Werkzeug (zuletzt benutztes der Gruppe), Pfeil rechts = Menü
      const wrap = el("div", "db-group");
      const b = iconBtn(g.id, g.label);
      const caret = iconBtn("caret", `${g.label} – alle Werkzeuge`, "db-caret");
      b.addEventListener("click", () => {
        const last = this.lastTool.get(g.id);
        if (g.id === "cursor" || !last) this.openFlyout(g.id, wrap);
        else this.pick(last, g.id);
      });
      caret.addEventListener("click", () => this.openFlyout(g.id, wrap));
      wrap.append(b, caret);
      this.groupBtns.set(g.id, b);
      root.append(wrap);
    }
    root.append(el("div", "db-sep"));
    const util = (id: string, title: string, fn: (b: HTMLButtonElement) => void) => {
      const b = iconBtn(id, title);
      b.addEventListener("click", () => fn(b));
      this.utilBtns.set(id, b);
      root.append(b);
    };
    util("measure", "Messen (auch Shift+Ziehen im Chart)", () => {
      this.c.setTool(null);
      this.c.measureMode = !this.c.measureMode;
      this.refresh();
    });
    util("zoom", "Zoom: Bereich aufziehen", () => {
      this.c.setTool(null);
      this.c.zoomMode = !this.c.zoomMode;
      this.refresh();
    });
    root.append(el("div", "db-sep"));
    util("magnet", "Magnet: aus → schwach (rastet nahe an O/H/L/C ein) → stark (immer)", () => {
      this.c.magnet = this.c.magnet === "off" ? "weak" : this.c.magnet === "weak" ? "strong" : "off";
      this.refresh();
    });
    util("keep", "Weiterzeichnen: Werkzeug bleibt nach dem Setzen aktiv", () => {
      this.c.keepDrawing = !this.c.keepDrawing;
      this.refresh();
    });
    util("lock", "Alle Zeichnungen sperren (nicht verschiebbar)", () => {
      this.c.lockAll = !this.c.lockAll;
      this.refresh();
    });
    util("eye", "Ausblenden: Zeichnungen / Positionen & Orders / alles · Objektliste", (b) => this.openHideMenu(b));
    root.append(el("div", "db-sep"));
    util("trash", "Alle Zeichnungen dieses Coins entfernen (Rückgängig: Strg+Z)", () => {
      if (this.c.items.length && confirm(`Alle ${this.c.items.length} Zeichnungen dieses Coins entfernen? (Rückgängig mit Strg+Z)`)) this.c.removeAll();
    });

    this.flyout = el("div", "db-flyout");
    this.flyout.hidden = true;
    document.body.append(this.flyout);
    document.addEventListener("pointerdown", (e) => {
      if (!this.flyout.hidden && !this.flyout.contains(e.target as Node) && !root.contains(e.target as Node)) this.flyout.hidden = true;
    });

    this.styleBar = el("div", "db-style");
    this.tree = el("div", "db-tree");
    styleHost.append(this.styleBar, this.tree);
    this.refresh();
  }

  private pick(id: string, group: GroupId): void {
    this.lastTool.set(group, id);
    this.c.setTool(this.c.tool === id ? null : id);
    this.flyout.hidden = true;
    this.refresh();
  }

  private openFlyout(group: GroupId, anchor: HTMLElement): void {
    const f = this.flyout;
    f.replaceChildren();
    if (group === "cursor") {
      for (const cur of CURSORS) {
        const b = el("button", `db-item${this.c.cursor === cur.id && !this.c.tool ? " on" : ""}`);
        b.type = "button";
        const ico = el("span", "db-ico");
        ico.innerHTML = ICONS[cur.icon] ?? "";
        b.append(ico, cur.label);
        b.addEventListener("click", () => {
          this.c.setCursor(cur.id);
          f.hidden = true;
          this.refresh();
        });
        f.append(b);
      }
    } else if (group === "icons") {
      // Emoji-Raster: Auswahl = Werkzeug "icon" mit diesem Text, ein Klick in den Chart setzt es
      const grid = el("div", "db-emoji");
      for (const e of EMOJIS) {
        const b = el("button", "db-emoji-btn", e);
        b.type = "button";
        b.addEventListener("click", () => {
          this.lastTool.set("icons", "icon");
          this.c.setTool("icon", e);
          f.hidden = true;
          this.refresh();
        });
        grid.append(b);
      }
      const own = el("button", "db-item", "Eigener Text / Emoji …");
      own.type = "button";
      own.addEventListener("click", () => this.pick("icon", "icons"));
      f.append(grid, own);
    } else {
      let section = "";
      for (const t of TOOLS.filter((x) => x.group === group) as readonly ToolDef[]) {
        if (t.section && t.section !== section) {
          section = t.section;
          f.append(el("div", "db-section", section));
        }
        const b = el("button", `db-item${this.c.tool === t.id ? " on" : ""}`, t.label);
        b.type = "button";
        b.title = pointsHint(t);
        b.addEventListener("click", () => this.pick(t.id, group));
        f.append(b);
      }
    }
    const r = anchor.getBoundingClientRect();
    f.style.left = `${r.right + 6}px`;
    f.style.top = `${Math.max(8, Math.min(r.top, window.innerHeight - 520))}px`;
    f.hidden = false;
  }

  private openHideMenu(anchor: HTMLElement): void {
    const f = this.flyout;
    f.replaceChildren();
    const opt = (label: string, on: boolean, fn: () => void) => {
      const b = el("button", `db-item${on ? " on" : ""}`, `${on ? "✓ " : ""}${label}`);
      b.type = "button";
      b.addEventListener("click", () => {
        fn();
        f.hidden = true;
        this.refresh();
      });
      f.append(b);
    };
    opt("Zeichnungen ausblenden", this.c.hideDrawings, () => (this.c.hideDrawings = !this.c.hideDrawings));
    opt("Positionen & Orders ausblenden", this.c.hideTrading, () => {
      this.c.hideTrading = !this.c.hideTrading;
      this.onHideTrading();
    });
    opt("Alles ausblenden", this.c.hideDrawings && this.c.hideTrading, () => {
      const all = !(this.c.hideDrawings && this.c.hideTrading);
      this.c.hideDrawings = all;
      this.c.hideTrading = all;
      this.onHideTrading();
    });
    opt("Objektliste anzeigen", this.treeOpen, () => (this.treeOpen = !this.treeOpen));
    const r = anchor.getBoundingClientRect();
    f.style.left = `${r.right + 6}px`;
    f.style.top = `${r.top}px`;
    f.hidden = false;
  }

  /** Zustand der Knöpfe, Stil-Leiste und Objektliste aktualisieren. */
  refresh(): void {
    const c = this.c;
    const activeGroup = c.tool ? TOOL_BY_ID.get(c.tool)?.group : null;
    for (const [g, b] of this.groupBtns) {
      b.classList.toggle("on", g === activeGroup || (g === "cursor" && !c.tool && !c.measureMode && !c.zoomMode && c.cursor !== "cross"));
      if (g === "cursor") b.innerHTML = ICONS[CURSORS.find((x) => x.id === c.cursor)?.icon ?? "cursor"] ?? "";
      if (g === activeGroup && c.tool) b.title = `${GROUPS.find((x) => x.id === g)!.label} – aktiv: ${TOOL_BY_ID.get(c.tool)!.label}`;
    }
    const on = (id: string, v: boolean) => this.utilBtns.get(id)!.classList.toggle("on", v);
    on("measure", c.measureMode);
    on("zoom", c.zoomMode);
    on("magnet", c.magnet !== "off");
    this.utilBtns.get("magnet")!.classList.toggle("strong", c.magnet === "strong");
    this.utilBtns.get("magnet")!.title = `Magnet: ${{ off: "aus", weak: "schwach", strong: "stark" }[c.magnet]} (Klick: aus → schwach → stark)`;
    on("keep", c.keepDrawing);
    on("lock", c.lockAll);
    this.utilBtns.get("lock")!.innerHTML = ICONS[c.lockAll ? "lockOn" : "lock"]!;
    on("eye", c.hideDrawings || c.hideTrading);
    this.utilBtns.get("eye")!.innerHTML = ICONS[c.hideDrawings || c.hideTrading ? "eyeOff" : "eye"]!;
    this.renderStyleBar();
    this.renderTree();
  }

  private renderStyleBar(): void {
    const d = this.c.selectedDrawing;
    const bar = this.styleBar;
    bar.replaceChildren();
    bar.hidden = !d;
    if (!d) return;
    const def = TOOL_BY_ID.get(d.tool);
    bar.append(el("span", "db-name", `${def?.label ?? d.tool}${d.by === "claude" ? " · von Claude" : ""}`));
    for (const col of COLORS) {
      const s = el("button", `db-swatch${d.style.color === col ? " on" : ""}`);
      s.type = "button";
      s.style.background = col;
      s.title = col;
      s.addEventListener("click", () => this.c.setStyle(d.id, { color: col }));
      bar.append(s);
    }
    const sel = <T extends string | number>(opts: [T, string][], cur: T, fn: (v: T) => void, title: string) => {
      const s = el("select", "db-select");
      s.title = title;
      for (const [v, l] of opts) {
        const o = el("option", "", l);
        o.value = String(v);
        o.selected = v === cur;
        s.append(o);
      }
      s.addEventListener("change", () => fn((typeof cur === "number" ? Number(s.value) : s.value) as T));
      bar.append(s);
    };
    sel<number>([[1, "1 px"], [1.5, "1,5 px"], [2, "2 px"], [3, "3 px"], [4, "4 px"]], d.style.width, (v) => this.c.setStyle(d.id, { width: v }), "Linienstärke");
    sel<Dash>([["solid", "———"], ["dashed", "– – –"], ["dotted", "· · ·"]], d.style.dash, (v) => this.c.setStyle(d.id, { dash: v }), "Linienart");
    const btn = (label: string, title: string, fn: () => void, on = false) => {
      const b = el("button", `db-small${on ? " on" : ""}`, label);
      b.type = "button";
      b.title = title;
      b.addEventListener("click", fn);
      bar.append(b);
    };
    if (["trend", "ray", "extended", "infoline", "parallel", "fibret", "fibext", "fibchannel"].includes(d.tool)) {
      btn("⇤", "Nach links verlängern", () => this.c.setStyle(d.id, { extendLeft: !d.style.extendLeft }), d.style.extendLeft);
      btn("⇥", "Nach rechts verlängern", () => this.c.setStyle(d.id, { extendRight: !d.style.extendRight }), d.style.extendRight);
    }
    if (d.style.fill !== null || ["rect", "circle", "ellipse", "triangle", "polyline", "rotrect"].includes(d.tool)) {
      btn("▨", "Füllung an/aus", () => this.c.setStyle(d.id, { fill: d.style.fill ? null : hexAlpha(d.style.color, 0.15) }), !!d.style.fill);
    }
    if (d.style.levels) {
      btn("Stufen", "Fib-/Gann-Stufen bearbeiten (Komma-Liste)", () => {
        const t = prompt("Stufen, mit Komma getrennt", d.style.levels!.join(", "));
        if (t === null) return;
        const lv = t.split(",").map((x) => Number(x.trim())).filter((x) => Number.isFinite(x));
        if (lv.length) this.c.setStyle(d.id, { levels: lv });
      });
    }
    btn("T", "Text bearbeiten", () => this.c.editText(d.id));
    btn("A±", "Schriftgröße", () => {
      const t = prompt("Schriftgröße (px)", String(d.style.fontSize));
      const n = Number(t);
      if (t !== null && n >= 6 && n <= 72) this.c.setStyle(d.id, { fontSize: n });
    });
    btn(d.locked ? "🔒" : "🔓", "Sperren", () => this.c.update(d.id, (x) => (x.locked = !x.locked)), !!d.locked);
    btn("⧉", "Duplizieren (auch Strg+C / Strg+V)", () => this.c.clone(d.id));
    btn("🗑", "Löschen (Entf)", () => this.c.remove(d.id));
  }

  private renderTree(): void {
    const t = this.tree;
    t.hidden = !this.treeOpen;
    if (!this.treeOpen) return;
    t.replaceChildren(el("div", "db-tree-h", `Objekte (${this.c.items.length})`));
    if (!this.c.items.length) t.append(el("div", "db-empty", "keine Zeichnungen"));
    for (const d of [...this.c.items].reverse()) {
      const row = el("div", `db-row${d.id === this.c.selected ? " on" : ""}`);
      const name = el("span", "db-row-name", label(d));
      name.addEventListener("click", () => {
        this.c.selected = d.id;
        this.c.version++;
        this.refresh();
      });
      const eye = el("button", "db-small", d.hidden ? "◌" : "👁");
      eye.title = "Ein-/ausblenden";
      eye.addEventListener("click", () => this.c.update(d.id, (x) => (x.hidden = !x.hidden)));
      const lock = el("button", "db-small", d.locked ? "🔒" : "🔓");
      lock.title = "Sperren";
      lock.addEventListener("click", () => this.c.update(d.id, (x) => (x.locked = !x.locked)));
      const del = el("button", "db-small", "×");
      del.title = "Löschen";
      del.addEventListener("click", () => this.c.remove(d.id));
      row.append(name, eye, lock, del);
      t.append(row);
    }
  }
}

function label(d: Drawing): string {
  const def = TOOL_BY_ID.get(d.tool);
  const txt = d.style.text ? ` „${d.style.text.slice(0, 18)}“` : "";
  return `${d.by === "claude" ? "🤖 " : ""}${def?.label ?? d.tool}${txt}`;
}

function pointsHint(t: ToolDef): string {
  if (t.points === "free") return "Mit gedrückter Maustaste zeichnen";
  if (t.points === "multi") return "Punkte klicken, Doppelklick oder Enter beendet";
  if (t.points === 1) return "Ein Klick";
  if (t.points === 2) return "Ziehen oder zwei Klicks";
  return `${t.points} Klicks`;
}

function hexAlpha(color: string, a: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(color);
  if (!m) return color;
  const n = parseInt(m[1]!, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}
