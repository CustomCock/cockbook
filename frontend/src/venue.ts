/**
 * Börse eines Symbol-Keys. Der Server benennt Symbole je Börse:
 * "BTCUSDT" = Binance USDⓈ-M, "BITUNIX:BTCUSDT" = Bitunix, "MULTI:BTCUSDT" = beide kombiniert.
 */

export type Venue = "binance" | "bitunix" | "multi";

export const VENUES: Venue[] = ["binance", "bitunix", "multi"];

export const VENUE_LABEL: Record<Venue, string> = {
  binance: "Binance",
  bitunix: "Bitunix",
  multi: "Kombiniert",
};

/** Kurzform für Tabs */
export const VENUE_SHORT: Record<Venue, string> = { binance: "", bitunix: "Bitunix", multi: "Σ Kombi" };

const PREFIX: Record<Venue, string> = { binance: "", bitunix: "BITUNIX:", multi: "MULTI:" };

export function venueOf(key: string): Venue {
  if (key.startsWith("BITUNIX:")) return "bitunix";
  if (key.startsWith("MULTI:")) return "multi";
  return "binance";
}

/** Coin ohne Börse, z. B. "MULTI:BTCUSDT" -> "BTCUSDT". */
export function coinOf(key: string): string {
  const i = key.indexOf(":");
  return i < 0 ? key : key.slice(i + 1);
}

export function keyFor(venue: Venue, coin: string): string {
  return PREFIX[venue] + coin;
}

/** Keys der Einzelbörsen eines kombinierten Keys (sonst nur der Key selbst). */
export function legsOf(key: string): string[] {
  if (venueOf(key) !== "multi") return [key];
  const c = coinOf(key);
  return [keyFor("binance", c), keyFor("bitunix", c)];
}
