/**
 * Alle Funktionen der Seitenleiste – orientiert am Funktionsumfang von Coinank,
 * beschränkt auf das, was sich aus öffentlichen, kostenlosen Börsen-Feeds bauen lässt.
 *
 * `status`: "live" = gebaut und schaltbar, "planned" = vorgesehen (mit Begründung).
 * Einstellungen gelten für alle Coins und werden im Browser gespeichert.
 */

export type FeatureGroup = "chart" | "history" | "derivatives" | "planned";

export interface Feature {
  id: string;
  label: string;
  group: FeatureGroup;
  status: "live" | "planned";
  defaultOn: boolean;
  hint: string;
}

export const FEATURES: Feature[] = [
  // --- Chart-Ebenen -----------------------------------------------------------------
  { id: "heatmap", label: "Liquidity-Heatmap", group: "chart", status: "live", defaultOn: true,
    hint: "Orderbuch-Liquidität je Preis über die Zeit" },
  { id: "smooth", label: "Weich zeichnen", group: "chart", status: "live", defaultOn: true,
    hint: "Weiche Übergänge zwischen Preis-Leveln statt harter Kanten" },
  { id: "bubbles", label: "Trade-Bubbles", group: "chart", status: "live", defaultOn: true,
    hint: "Aggressive Käufe (grün) / Verkäufe (rot), Fläche ~ Gegenwert in USDT" },
  { id: "bbo", label: "Best Bid / Ask", group: "chart", status: "live", defaultOn: true,
    hint: "Bester Kauf- und Verkaufspreis als Linie" },
  { id: "liqMarkers", label: "Liquidationen im Chart", group: "chart", status: "live", defaultOn: true,
    hint: "Rauten: orange = Long liquidiert, blau = Short liquidiert" },
  { id: "grid", label: "Preisgitter", group: "chart", status: "live", defaultOn: true,
    hint: "Horizontale Hilfslinien" },
  { id: "candles", label: "Kerzen", group: "chart", status: "live", defaultOn: false,
    hint: "OHLC-Kerzen über der Heatmap (1m-Basis von Binance; 3m/5m/10m daraus gebaut)" },

  // --- Langfristig: ATH & Zyklen ---------------------------------------------------------
  { id: "ath", label: "Allzeithoch & Jahr", group: "history", status: "live", defaultOn: true,
    hint: "ATH, Abstand, Tage seit ATH, 52-Wochen-Spanne, Performance seit 1. Januar (Spot-Tageskerzen)" },
  { id: "cycles", label: "Marktzyklus", group: "history", status: "live", defaultOn: true,
    hint: "Halving-Zyklen oder eigener Zeitraum: Länge, Fortschritt, Zyklus-Hoch/-Tief, Performance" },

  // --- Derivate-Kennzahlen (Coinank: Funding, OI, Longs vs Shorts, Liquidationen) ------
  { id: "markPrice", label: "Mark- & Indexpreis", group: "derivatives", status: "live", defaultOn: true,
    hint: "Binance markPrice-Stream, sekündlich" },
  { id: "funding", label: "Funding Rate", group: "derivatives", status: "live", defaultOn: true,
    hint: "Aktuelle Rate, annualisiert, Countdown bis zum nächsten Funding" },
  { id: "openInterest", label: "Open Interest", group: "derivatives", status: "live", defaultOn: true,
    hint: "Offene Kontrakte + Veränderung über 5 min / 1 h (seit Start der Sitzung)" },
  { id: "longShort", label: "Longs vs Shorts", group: "derivatives", status: "live", defaultOn: true,
    hint: "Anteil Accounts netto long/short (alle Binance-Nutzer, 5-min-Periode)" },
  { id: "takerVolume", label: "Taker Buy/Sell", group: "derivatives", status: "live", defaultOn: true,
    hint: "Aggressives Kauf- vs. Verkaufsvolumen (5-min-Periode)" },
  { id: "ticker24h", label: "24h-Statistik", group: "derivatives", status: "live", defaultOn: true,
    hint: "Hoch, Tief, Volumen der letzten 24 h" },
  { id: "liqFeed", label: "Liquidationen (Gesamtmarkt)", group: "derivatives", status: "live", defaultOn: true,
    hint: "Alle USDⓈ-M-Coins, sekündlich: Balken je Sekunde, Summen 1 min / 1 h, Top-Coins, Liste. Binance liefert max. 1 Liquidation pro Sekunde und Coin" },

  // --- Coinank-Funktionen, noch nicht gebaut ---------------------------------------------
  { id: "liqHeatmap", label: "Liquidation Heatmap / Map", group: "planned", status: "planned", defaultOn: false,
    hint: "Geschätzte Liquidationslevel aus OI-Änderungen + Hebelannahmen (Modell, keine Börsendaten)" },
  { id: "fundingHeatmap", label: "Funding-Heatmap & kumuliertes Funding", group: "planned", status: "planned", defaultOn: false,
    hint: "Funding-Historie aller Coins per REST" },
  { id: "rsiHeatmap", label: "RSI-Heatmap / Screener", group: "planned", status: "planned", defaultOn: false,
    hint: "Aus Kerzen berechnet – Phase 6 (Indikatoren)" },
  { id: "depthDelta", label: "Orderbook Depth Delta", group: "planned", status: "planned", defaultOn: false,
    hint: "Bid/Ask-Ungleichgewicht im Buch – Phase 6 (Imbalance)" },
  { id: "aggBook", label: "Aggregiertes Orderbuch / Börsen-Vergleich", group: "planned", status: "planned", defaultOn: false,
    hint: "Mehrere Börsen (Bybit, OKX, Coinbase) – Phase 5 (Multibook)" },
  { id: "topTrader", label: "Top-Trader Long/Short", group: "planned", status: "planned", defaultOn: false,
    hint: "Binance verlangt dafür inzwischen einen API-Key" },
  { id: "offExchange", label: "ETF, Fund Flow, Whales, Fear & Greed, On-Chain", group: "planned", status: "planned", defaultOn: false,
    hint: "Keine Börsen-Feeds – außerhalb des Projektrahmens" },
];

const KEY = "cockbook.features.v1";

export type FeatureState = Record<string, boolean>;

export function loadFeatures(): FeatureState {
  const state: FeatureState = Object.fromEntries(FEATURES.map((f) => [f.id, f.defaultOn]));
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) Object.assign(state, JSON.parse(raw) as FeatureState);
  } catch {
    /* privates Fenster o. Ä. */
  }
  for (const f of FEATURES) if (f.status !== "live") state[f.id] = false;
  return state;
}

export function saveFeatures(state: FeatureState): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    /* egal */
  }
}
