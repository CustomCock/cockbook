/**
 * Symbole der Zeichenleiste: eigene, schlichte Linien-Icons (28 x 28, Strichfarbe =
 * currentColor). Bewusst als Inline-SVG statt Emoji – sieht auf allen Systemen gleich aus
 * und übernimmt Hover-/Aktiv-Farben aus dem CSS.
 */

const svg = (body: string) =>
  `<svg viewBox="0 0 28 28" width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

const dot = (x: number, y: number, r = 1.8) => `<circle cx="${x}" cy="${y}" r="${r}"/>`;

export const ICONS: Record<string, string> = {
  // Fadenkreuz mit Lücke in der Mitte
  cursor: svg(`<path d="M14 5v6.5M14 16.5V23M5 14h6.5M16.5 14H23"/>`),
  dot: svg(`<circle cx="14" cy="14" r="2.2" fill="currentColor"/>`),
  arrow: svg(`<path d="M9 6l11 8.5-5.2.8 3 5.6-2 1.1-3-5.6L9 20z"/>`),
  demo: svg(`<path d="M6 20c3-1 4-6 7-6s3 4 6 4 3-5 4-7"/>${dot(23, 11, 1.4)}`),
  magic: svg(`<path d="M7 21L18 10M17 6v3M21 10h-3M20 7l-1.6 1.6M12 6.5v1.5M22 14.5h-1.5"/>`),
  eraser: svg(`<path d="M6 18.5l8.5-8.5 5.5 5.5-5 5H9.5zM11 13l5.5 5.5M13 23h9"/>`),
  // Trendlinie mit Ankerpunkten
  lines: svg(`<path d="M8.3 19.7L19.7 8.3"/>${dot(7, 21)}${dot(21, 7)}`),
  // Retracement-Stufen
  fib: svg(`<path d="M5 7.5h18M5 12h18M5 16.5h18M5 21h18"/>${dot(9, 7.5, 1.6)}${dot(19, 21, 1.6)}<path d="M9 9.1l10 10.3" stroke-dasharray="1.5 2"/>`),
  // XABCD: Zickzack mit Knoten
  patterns: svg(`<path d="M5.5 20.5l5-12 5 8 4-10 3 13M10.5 8.5l9-2M5.5 20.5l17-1" />${dot(5.5, 20.5, 1.5)}${dot(10.5, 8.5, 1.5)}${dot(15.5, 16.5, 1.5)}${dot(19.5, 6.5, 1.5)}${dot(22.5, 19.5, 1.5)}`),
  // Long-Position: Einstieg, Ziel, Stop
  forecast: svg(`<path d="M6 6.5h16M6 14h16M6 21.5h16M6 6.5v15"/><path d="M10 10h8M10 18h5" stroke-dasharray="1.5 2"/>`),
  // Pinsel
  shapes: svg(`<path d="M15.5 12.5l6-6.5 1.5 1.5-6.5 6M15.5 12.5l1.5 1.5"/><path d="M15.5 12.5c-2.5-.5-4.5 1-4.5 3.5 0 2-2 3-5 3 2 3 9 3 10.5-1.5.4-1.4.2-2.6-1-5z"/>`),
  notes: svg(`<path d="M7 7h14M14 7v15M11.5 22h5"/>`),
  icons: svg(`<circle cx="14" cy="14" r="8.5"/><path d="M10.5 16c1.8 2.4 5.2 2.4 7 0"/>${dot(11, 11.8, 0.9)}${dot(17, 11.8, 0.9)}`),
  // Lineal
  measure: svg(`<path d="M4.5 18.5L18.5 4.5l5 5-14 14zM8 15l2 2M10.5 12.5l1.5 1.5M13 10l2 2M15.5 7.5l1.5 1.5"/>`),
  zoom: svg(`<circle cx="12.5" cy="12.5" r="6.5"/><path d="M17.3 17.3L23 23M12.5 9.5v6M9.5 12.5h6"/>`),
  magnet: svg(`<path d="M8 6v8a6 6 0 0 0 12 0V6h-4v8a2 2 0 0 1-4 0V6zM8 9.5h4M16 9.5h4"/>`),
  // Stift mit kleinem Schloss: Werkzeug bleibt aktiv
  keep: svg(`<path d="M6 20l1-4 9.5-9.5 3 3L10 19zM14.5 8.5l3 3"/><rect x="17" y="19" width="6" height="4.5" rx="0.8"/><path d="M18.3 19v-1.5a1.7 1.7 0 0 1 3.4 0V19"/>`),
  lock: svg(`<rect x="8" y="13" width="12" height="9" rx="1.5"/><path d="M10.5 13V9.5a3.5 3.5 0 0 1 7 0"/>`),
  lockOn: svg(`<rect x="8" y="13" width="12" height="9" rx="1.5"/><path d="M10.5 13V9.5a3.5 3.5 0 0 1 7 0V13"/>`),
  eye: svg(`<path d="M3.5 14S7.5 7.5 14 7.5 24.5 14 24.5 14 20.5 20.5 14 20.5 3.5 14 3.5 14z"/><circle cx="14" cy="14" r="3"/>`),
  eyeOff: svg(`<path d="M3.5 14S7.5 7.5 14 7.5 24.5 14 24.5 14 20.5 20.5 14 20.5 3.5 14 3.5 14z"/><circle cx="14" cy="14" r="3"/><path d="M5 23L23 5"/>`),
  trash: svg(`<path d="M6 8h16M11 8V6h6v2M8 8l1 15h10l1-15M12 12v7M16 12v7"/>`),
  tree: svg(`<path d="M10 8h12M10 14h12M10 20h12"/>${dot(6, 8, 1)}${dot(6, 14, 1)}${dot(6, 20, 1)}`),
  caret: `<svg viewBox="0 0 8 8" width="8" height="8" fill="currentColor" aria-hidden="true"><path d="M2 1l4 3-4 3z"/></svg>`,
};
