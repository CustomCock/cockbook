/** Gruppen im Coin-Menü/Tabs und Texte zu TradFi-Handelszeiten. */

import type { Session } from "./markets";
import type { Category } from "./net";

export const CATEGORY_GROUPS: { id: Category; title: string }[] = [
  { id: "crypto", title: "Krypto" },
  { id: "stock", title: "Aktien (TradFi)" },
  { id: "etf", title: "ETFs (TradFi)" },
  { id: "commodity", title: "Rohstoffe (TradFi)" },
  { id: "fx", title: "Devisen (TradFi)" },
  { id: "tradfi", title: "Sonstige TradFi" },
];

/** Session-Typen laut Binance-Doku (tradingSession-Stream). */
const SESSION_TEXT: Record<string, string> = {
  REGULAR: "offen",
  NO_TRADING: "geschlossen",
  PRE_MARKET: "Vorbörse",
  AFTER_MARKET: "Nachbörse",
  OVERNIGHT: "Nachthandel",
};

export function sessionText(s: Session | null): string {
  if (!s) return "";
  return SESSION_TEXT[s.session] ?? s.session;
}

/** Kurzer Text mit Ende der Phase, z. B. "geschlossen bis Mo., 15:30". */
export function sessionLong(s: Session | null): string {
  if (!s) return "Handelszeit unbekannt";
  const until = s.end
    ? ` bis ${new Date(s.end).toLocaleString("de-DE", { weekday: "short", hour: "2-digit", minute: "2-digit" })}`
    : "";
  return `${sessionText(s)}${until}`;
}

/** CSS-Klasse: "open" (regulär), "ext" (Vor-/Nach-/Nachthandel), "closed". */
export function sessionClass(s: Session | null): string {
  if (!s) return "";
  if (s.session === "REGULAR") return "open";
  if (s.session === "NO_TRADING") return "closed";
  return "ext";
}
