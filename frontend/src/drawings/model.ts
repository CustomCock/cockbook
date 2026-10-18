/**
 * Zeichnungen im Chart (Werkzeugleiste links, Umfang orientiert an TradingView).
 *
 * Ankerpunkte sind **Zeit + Preis**, nicht Pixel: Zeichnungen bleiben beim Zoomen und
 * Scrollen an ihrer Stelle und gelten für alle Börsen desselben Coins (gespeichert je Coin,
 * z. B. "BTCUSDT" – auf Binance, Bitunix und im kombinierten Buch gleich).
 *
 * Gespeichert werden sie im Backend (data/drawings/), nicht im Browser: So sehen alle
 * offenen Tabs dieselben Zeichnungen, und Claude kann über die Erweiterung Zeichnungen
 * lesen und setzen.
 */

export interface Pt {
  /** Zeit in ms (UTC) */
  t: number;
  /** Preis */
  p: number;
}

export type Dash = "solid" | "dashed" | "dotted";

export interface Style {
  color: string;
  width: number;
  dash: Dash;
  /** Füllung (CSS-Farbe) oder null */
  fill: string | null;
  text: string;
  fontSize: number;
  extendLeft: boolean;
  extendRight: boolean;
  /** Fibonacci-/Gann-Stufen (Werkzeuge mit Stufen) */
  levels: number[] | null;
}

export interface Drawing {
  id: string;
  tool: ToolId;
  pts: Pt[];
  style: Style;
  locked?: boolean;
  hidden?: boolean;
  /** "claude" = über die Claude-Erweiterung gesetzt */
  by?: "user" | "claude";
  created: number;
}

export type GroupId = "cursor" | "lines" | "fib" | "patterns" | "forecast" | "shapes" | "notes";

export const GROUPS: { id: GroupId; label: string; icon: string }[] = [
  { id: "cursor", label: "Cursor", icon: "✛" },
  { id: "lines", label: "Linien, Kanäle, Pitchforks", icon: "╱" },
  { id: "fib", label: "Fibonacci & Gann", icon: "𝜑" },
  { id: "patterns", label: "Muster & Zyklen", icon: "⋀" },
  { id: "forecast", label: "Prognose & Messen", icon: "⇕" },
  { id: "shapes", label: "Formen, Pinsel, Pfeile", icon: "▭" },
  { id: "notes", label: "Text & Notizen", icon: "T" },
];

/**
 * Anzahl der Ankerpunkte: Zahl = fest; "free" = Freihand (Pinsel, Ziehen); "multi" = beliebig
 * viele (Doppelklick oder Enter beendet).
 */
export type PointSpec = number | "free" | "multi";

export interface ToolDef {
  id: string;
  label: string;
  group: GroupId;
  points: PointSpec;
  /** Untergruppe im Menü */
  section?: string;
  /** Bei Text-Werkzeugen beim Setzen nach dem Text fragen */
  askText?: boolean;
  defaults?: Partial<Style>;
}

export const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.618, 2.618];
export const FIB_EXT_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.272, 1.618, 2, 2.618];
export const GANN_LEVELS = [0, 0.25, 0.382, 0.5, 0.618, 0.75, 1];

const T = (id: string, label: string, group: GroupId, points: PointSpec, extra: Partial<ToolDef> = {}): ToolDef => ({
  id,
  label,
  group,
  points,
  ...extra,
});

export const TOOLS = [
  // Linien
  T("trend", "Trendlinie", "lines", 2, { section: "Linien" }),
  T("ray", "Strahl", "lines", 2, { section: "Linien", defaults: { extendRight: true } }),
  T("infoline", "Info-Linie", "lines", 2, { section: "Linien" }),
  T("extended", "Verlängerte Linie", "lines", 2, { section: "Linien", defaults: { extendLeft: true, extendRight: true } }),
  T("trendangle", "Trendwinkel", "lines", 2, { section: "Linien" }),
  T("hline", "Horizontale Linie", "lines", 1, { section: "Linien" }),
  T("hray", "Horizontaler Strahl", "lines", 1, { section: "Linien" }),
  T("vline", "Vertikale Linie", "lines", 1, { section: "Linien" }),
  T("crossline", "Kreuzlinie", "lines", 1, { section: "Linien" }),
  // Kanäle
  T("parallel", "Paralleler Kanal", "lines", 3, { section: "Kanäle", defaults: { fill: "rgba(90,204,255,0.08)" } }),
  T("regression", "Regressionstrend", "lines", 2, { section: "Kanäle", defaults: { fill: "rgba(90,204,255,0.06)" } }),
  T("flattop", "Flacher Kanal (Top/Bottom)", "lines", 3, { section: "Kanäle", defaults: { fill: "rgba(90,204,255,0.08)" } }),
  T("disjoint", "Getrennter Kanal", "lines", 4, { section: "Kanäle", defaults: { fill: "rgba(90,204,255,0.08)" } }),
  // Pitchforks
  T("pitchfork", "Pitchfork", "lines", 3, { section: "Pitchforks" }),
  T("schiff", "Schiff-Pitchfork", "lines", 3, { section: "Pitchforks" }),
  T("modschiff", "Modifizierte Schiff-Pitchfork", "lines", 3, { section: "Pitchforks" }),
  T("insidefork", "Innere Pitchfork", "lines", 3, { section: "Pitchforks" }),
  // Fibonacci
  T("fibret", "Fibonacci-Retracement", "fib", 2, { section: "Fibonacci", defaults: { levels: FIB_LEVELS } }),
  T("fibext", "Trendbasierte Fib-Extension", "fib", 3, { section: "Fibonacci", defaults: { levels: FIB_EXT_LEVELS } }),
  T("fibchannel", "Fib-Kanal", "fib", 3, { section: "Fibonacci", defaults: { levels: FIB_LEVELS } }),
  T("fibtimezone", "Fib-Zeitzonen", "fib", 2, { section: "Fibonacci" }),
  T("fibfan", "Fib-Speed-Resistance-Fächer", "fib", 2, { section: "Fibonacci", defaults: { levels: [0.25, 0.382, 0.5, 0.618, 0.75] } }),
  T("fibtime", "Trendbasierte Fib-Zeit", "fib", 3, { section: "Fibonacci", defaults: { levels: [0, 0.382, 0.5, 0.618, 1, 1.382, 1.618, 2, 2.618] } }),
  T("fibcircles", "Fib-Kreise", "fib", 2, { section: "Fibonacci", defaults: { levels: [0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.618] } }),
  T("fibspiral", "Fib-Spirale", "fib", 2, { section: "Fibonacci" }),
  T("fibarcs", "Fib-Speed-Resistance-Bögen", "fib", 2, { section: "Fibonacci", defaults: { levels: [0.236, 0.382, 0.5, 0.618, 0.786, 1] } }),
  T("fibwedge", "Fib-Keil", "fib", 3, { section: "Fibonacci", defaults: { levels: [0.236, 0.382, 0.5, 0.618, 0.786, 1] } }),
  T("pitchfan", "Pitchfan", "fib", 3, { section: "Fibonacci", defaults: { levels: [0.25, 0.382, 0.5, 0.618, 0.75] } }),
  // Gann
  T("gannbox", "Gann-Box", "fib", 2, { section: "Gann", defaults: { levels: GANN_LEVELS } }),
  T("gannsqfixed", "Gann-Quadrat (fest)", "fib", 1, { section: "Gann" }),
  T("gannsq", "Gann-Quadrat", "fib", 2, { section: "Gann" }),
  T("gannfan", "Gann-Fächer", "fib", 2, { section: "Gann" }),
  // Muster
  T("xabcd", "XABCD-Muster", "patterns", 5, { section: "Chartmuster", defaults: { fill: "rgba(185,156,255,0.12)" } }),
  T("cypher", "Cypher-Muster", "patterns", 5, { section: "Chartmuster", defaults: { fill: "rgba(185,156,255,0.12)" } }),
  T("headshoulders", "Schulter-Kopf-Schulter", "patterns", 7, { section: "Chartmuster", defaults: { fill: "rgba(185,156,255,0.10)" } }),
  T("abcd", "ABCD-Muster", "patterns", 4, { section: "Chartmuster" }),
  T("trianglepattern", "Dreiecksmuster", "patterns", 4, { section: "Chartmuster", defaults: { fill: "rgba(185,156,255,0.10)" } }),
  T("threedrives", "Three Drives", "patterns", 7, { section: "Chartmuster" }),
  // Elliott
  T("elliottimpulse", "Elliott-Impulswelle (12345)", "patterns", 6, { section: "Elliott-Wellen" }),
  T("elliottcorrection", "Elliott-Korrektur (ABC)", "patterns", 4, { section: "Elliott-Wellen" }),
  T("elliotttriangle", "Elliott-Dreieck (ABCDE)", "patterns", 6, { section: "Elliott-Wellen" }),
  T("elliottdouble", "Elliott-Doppelkombination (WXY)", "patterns", 4, { section: "Elliott-Wellen" }),
  T("elliotttriple", "Elliott-Dreifachkombination (WXYXZ)", "patterns", 6, { section: "Elliott-Wellen" }),
  // Zyklen
  T("cycliclines", "Zyklische Linien", "patterns", 2, { section: "Zyklen" }),
  T("timecycles", "Zeitzyklen", "patterns", 2, { section: "Zyklen" }),
  T("sine", "Sinuslinie", "patterns", 2, { section: "Zyklen" }),
  // Prognose
  T("long", "Long-Position", "forecast", 1, { section: "Prognose" }),
  T("short", "Short-Position", "forecast", 1, { section: "Prognose" }),
  T("forecast", "Positionsprognose", "forecast", 2, { section: "Prognose" }),
  T("barspattern", "Kerzenmuster kopieren", "forecast", 3, { section: "Prognose" }),
  T("ghostfeed", "Ghost Feed", "forecast", "multi", { section: "Prognose" }),
  T("sector", "Sektor", "forecast", 3, { section: "Prognose", defaults: { fill: "rgba(90,204,255,0.10)" } }),
  // Volumen
  T("avwap", "Anchored VWAP", "forecast", 1, { section: "Volumen" }),
  T("fixedvp", "Volumenprofil (fester Bereich)", "forecast", 2, { section: "Volumen" }),
  T("anchoredvp", "Volumenprofil (verankert)", "forecast", 1, { section: "Volumen" }),
  // Messen
  T("pricerange", "Preisbereich", "forecast", 2, { section: "Messen" }),
  T("daterange", "Zeitbereich", "forecast", 2, { section: "Messen" }),
  T("dateprice", "Zeit- und Preisbereich", "forecast", 2, { section: "Messen" }),
  // Formen
  T("rect", "Rechteck", "shapes", 2, { section: "Formen", defaults: { fill: "rgba(90,204,255,0.12)" } }),
  T("rotrect", "Gedrehtes Rechteck", "shapes", 3, { section: "Formen", defaults: { fill: "rgba(90,204,255,0.12)" } }),
  T("path", "Pfad", "shapes", "multi", { section: "Formen" }),
  T("circle", "Kreis", "shapes", 2, { section: "Formen", defaults: { fill: "rgba(90,204,255,0.12)" } }),
  T("ellipse", "Ellipse", "shapes", 2, { section: "Formen", defaults: { fill: "rgba(90,204,255,0.12)" } }),
  T("polyline", "Polylinie", "shapes", "multi", { section: "Formen", defaults: { fill: "rgba(90,204,255,0.10)" } }),
  T("triangle", "Dreieck", "shapes", 3, { section: "Formen", defaults: { fill: "rgba(90,204,255,0.12)" } }),
  T("arc", "Bogen", "shapes", 3, { section: "Formen" }),
  T("curve", "Kurve", "shapes", 3, { section: "Formen" }),
  T("doublecurve", "Doppelkurve", "shapes", 4, { section: "Formen" }),
  // Pinsel & Pfeile
  T("brush", "Pinsel", "shapes", "free", { section: "Pinsel & Pfeile" }),
  T("highlighter", "Textmarker", "shapes", "free", { section: "Pinsel & Pfeile", defaults: { color: "rgba(255,226,79,0.35)", width: 14 } }),
  T("arrow", "Pfeil", "shapes", 2, { section: "Pinsel & Pfeile" }),
  T("arrowmarker", "Pfeilmarker", "shapes", 2, { section: "Pinsel & Pfeile", defaults: { fill: "rgba(90,204,255,0.85)" } }),
  T("arrowup", "Pfeil nach oben", "shapes", 1, { section: "Pinsel & Pfeile", defaults: { color: "#55e5a8" } }),
  T("arrowdown", "Pfeil nach unten", "shapes", 1, { section: "Pinsel & Pfeile", defaults: { color: "#ff7385" } }),
  T("arrowleft", "Pfeil nach links", "shapes", 1, { section: "Pinsel & Pfeile" }),
  T("arrowright", "Pfeil nach rechts", "shapes", 1, { section: "Pinsel & Pfeile" }),
  // Text & Notizen
  T("text", "Text", "notes", 1, { section: "Text", askText: true }),
  T("note", "Notiz", "notes", 1, { section: "Text", askText: true }),
  T("pricenote", "Preisnotiz", "notes", 2, { section: "Text", askText: true }),
  T("pin", "Stecknadel", "notes", 1, { section: "Text", askText: true }),
  T("table", "Tabelle", "notes", 1, { section: "Text", askText: true }),
  T("callout", "Sprechblase mit Zeiger", "notes", 2, { section: "Text", askText: true }),
  T("comment", "Kommentar", "notes", 1, { section: "Text", askText: true }),
  T("pricelabel", "Preis-Label", "notes", 1, { section: "Text" }),
  T("signpost", "Wegweiser", "notes", 1, { section: "Text", askText: true }),
  T("icon", "Icon / Emoji", "notes", 1, { section: "Text", askText: true, defaults: { fontSize: 22 } }),
] as const satisfies readonly ToolDef[];

export type ToolId = (typeof TOOLS)[number]["id"];

export const TOOL_BY_ID: Map<string, ToolDef> = new Map(TOOLS.map((t) => [t.id, t]));

export const DEFAULT_STYLE: Style = {
  color: "#5accff",
  width: 1.5,
  dash: "solid",
  fill: null,
  text: "",
  fontSize: 12,
  extendLeft: false,
  extendRight: false,
  levels: null,
};

export function styleFor(tool: string, override: Partial<Style> = {}): Style {
  const def = TOOL_BY_ID.get(tool);
  return { ...DEFAULT_STYLE, ...(def?.defaults ?? {}), ...override };
}

/** Schlüssel, unter dem Zeichnungen gespeichert werden: der Coin ohne Börse. */
export function drawingKey(symbol: string): string {
  const i = symbol.indexOf(":");
  return (i < 0 ? symbol : symbol.slice(i + 1)).replace(/@.*$/, "");
}

export function newId(): string {
  return `d${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
}
