/** WebSocket-Verbindung zum Backend mit Reconnect. */

import { decode } from "@msgpack/msgpack";

export interface Hello {
  type: "hello";
  mode: "live" | "replay";
  exchange: string;
  symbol: string;
  tick_size: string;
  bucket_ticks: number;
  interval_ms: number;
  /** Stufen der Langzeit-Ansicht, z. B. ["12h", "1D", "1W", "1M"] */
  long_tiers?: string[];
  half_range_buckets: number;
  ui: {
    recenter_margin: number;
    bubble_px_per_sqrt_kusd: number;
    bubble_max_px: number;
    initial_view_buckets?: number;
  };
}

export interface Status {
  type: "status";
  symbol: string;
  synced: boolean;
  reason: string;
}

export interface SymbolInfo {
  symbol: string;
  exchange?: string;
  tick_size?: string;
  /** Mengen-Schrittweite (kleinste Ordermenge) */
  step_size?: string;
  kind?: "crypto" | "tradfi";
  /** crypto | stock | etf | commodity | fx | tradfi */
  category?: Category;
  /** TradFi: Markt der Handelszeiten (z. B. EQUITY, COMMODITY) */
  market?: string;
  label?: string;
}

export type Category = "crypto" | "stock" | "etf" | "commodity" | "fx" | "tradfi";

export interface Symbols {
  type: "symbols";
  symbols: SymbolInfo[];
}

/** Chart-Frames (nur für das abonnierte Symbol). */
export type ChartFrame =
  | {
      t: "col";
      sym: string;
      ts: number;
      s: boolean;
      b: number;
      c?: number | null;
      /** Schlüsselbild (vollständig) oder nur Änderungen; `n` = laufende Nummer */
      k?: boolean;
      n?: number;
      q: Uint8Array;
      i?: Uint8Array;
    }
  | { t: "bbo"; sym: string; ts: number; bb: number | null; ba: number | null }
  | { t: "tr"; sym: string; ts: number; x: [number, number, number, number][] }
  | { t: "lq"; sym: string; ts: number; x: [number, number, number][] }
  | { t: "kh"; sym: string; x: number[][] }
  | { t: "k"; sym: string; x: [number, number, number, number, number, number, boolean] };

/** Kennzahl-Frames (alle Symbole). */
export type StatFrame =
  | { t: "mark"; sym: string; ts: number; mp: number; ip: number | null; fr: number; nft: number | null }
  | {
      t: "tick";
      sym: string;
      ts: number;
      last: number;
      open: number;
      high: number;
      low: number;
      vol: number;
      qvol: number;
    }
  | { t: "oi"; sym: string; ts: number; oi: number }
  | { t: "ls"; sym: string; ts: number; p: string; long: number; short: number; ratio: number }
  | { t: "tv"; sym: string; ts: number; p: string; buy: number; sell: number; ratio: number }
  | { t: "liq"; sym: string; ts: number; side: 1 | -1; price: number; qty: number }
  | { t: "d1"; sym: string; x: [number, number, number, number, number, number][] }
  | { t: "sync"; sym: string; ts: number; synced: boolean; resyncs: number; reason: string }
  /** TradFi-Handelszeit; `sym` ist hier der Markt (z. B. "COMMODITY"), kein Symbol */
  | { t: "sess"; sym: string; ts: number; session: string; start: number | null; end: number | null };

export type BinaryFrame = ChartFrame | StatFrame;

export function isChartFrame(f: BinaryFrame): f is ChartFrame {
  return f.t === "col" || f.t === "bbo" || f.t === "tr" || f.t === "lq" || f.t === "kh" || f.t === "k";
}

export interface Handlers {
  onSymbols(s: Symbols): void;
  onHello(h: Hello): void;
  onStatus(s: Status): void;
  onChart(f: ChartFrame): void;
  onStat(f: StatFrame): void;
  /** Langzeit-Ansicht: Steuer-Nachrichten und Frames */
  onLong(m: LongMessage): void;
  /** Server lehnt ab (Token fehlt/falsch): nicht automatisch neu verbinden */
  onAuthError(): void;
  onConnection(connected: boolean): void;
  /** Paper-Trading: Zustand, Antwort auf eine Anfrage, Hinweis (z. B. TP ausgeführt) */
  onPaper?(m: PaperMessage): void;
  /** Echtes Börsenkonto */
  onLive?(m: LiveState): void;
  onLiveResult?(m: LiveResult): void;
}

/** Antwort auf eine Live-Anfrage (`data` z. B. Hebel/Margin-Modus bei "settings"). */
export interface LiveResult {
  type: "live_result";
  req: number | null;
  ok: boolean;
  message: string;
  /** settings: `leverage`/`margin_mode` null = Coin bei Bitunix noch nie eingestellt */
  data: { symbol?: string; leverage?: number | null; margin_mode?: string | null };
}

/** Echtes Börsenkonto (Stufe 1: nur lesen). */
export interface LiveState {
  type: "live_state";
  exchange: string;
  /** no_keys | off | connecting | ok | error */
  status: string;
  message: string;
  ws: boolean;
  key: string | null;
  updated: number;
  /** true = Live-Trading in config.yaml aus (nur lesen) */
  read_only: boolean;
  max_leverage: number;
  tpsl_trigger?: string;
  account?: { available: number; frozen: number; margin: number; upnl: number; equity: number; transfer: number | null; bonus: number | null; position_mode: string | null };
  positions?: {
    symbol: string;
    position_id: string;
    qty: number;
    entry: number | null;
    mark: number | null;
    upnl: number;
    roe: number;
    realized: number;
    fee: number;
    funding: number;
    leverage: number;
    margin: number;
    margin_mode: string | null;
    liq: number | null;
    tp: number | null;
    sl: number | null;
    opened: number;
    tpsl_ids?: string[];
  }[];
  orders?: { id: string; symbol: string; side: "buy" | "sell"; kind: string; qty: number; filled: number; price: number | null; reduce_only: boolean; tp: number | null; sl: number | null; status: string; created: number }[];
  warnings?: string[];
}

export type PaperMessage =
  | PaperState
  | { type: "paper_result"; req: number | null; ok: boolean; message: string }
  | { type: "paper_event"; level: "ok" | "error"; message: string };

export interface PaperPosition {
  symbol: string;
  qty: number; // > 0 long, < 0 short
  entry: number;
  mark: number;
  upnl: number;
  roe: number;
  leverage: number;
  margin: number;
  liq: number;
  tp: number | null;
  sl: number | null;
  opened: number;
  breakeven: number;
}

export interface PaperOrder {
  id: string;
  symbol: string;
  side: "buy" | "sell";
  kind: "market" | "limit" | "stop";
  qty: number;
  filled: number;
  price: number | null;
  trigger: number | null;
  reduce_only: boolean;
  tp: number | null;
  sl: number | null;
  queue: number;
  created: number;
}

export interface PaperTrade {
  symbol: string;
  side: "long" | "short";
  qty: number;
  entry: number;
  exit: number;
  pnl: number;
  fees: number;
  opened: number;
  closed: number;
  reason: string;
}

export interface PaperState {
  type: "paper_state";
  account: {
    wallet: number;
    equity: number;
    upnl: number;
    margin: number;
    available: number;
    day_realized: number;
    start: number;
    max_leverage: number;
    default_leverage: number;
    loss_limit: number;
    loss_limit_hit: boolean;
  };
  leverage: Record<string, number>;
  positions: PaperPosition[];
  orders: PaperOrder[];
  fills: { ts: number; order: string; symbol: string; side: "buy" | "sell"; qty: number; price: number; fee: number; liq: string; realized: number; reason: string }[];
  trades: PaperTrade[];
}

/** float32-LE-Bytes -> Float32Array. Kopie nötig: msgpack liefert eine Sicht mit
 *  beliebigem Byte-Offset, Float32Array braucht 4-Byte-Ausrichtung. */
export function toFloat32(bytes: Uint8Array): Float32Array {
  return new Float32Array(bytes.slice().buffer);
}

export function toInt32(bytes: Uint8Array): Int32Array {
  return new Int32Array(bytes.slice().buffer);
}

/** Langzeit-Ansicht (eigener Tab). */
export interface LongHello {
  type: "long_hello";
  symbol: string;
  tier: string;
  interval_ms: number;
  span_ms: number;
  factor: number;
  bucket_ticks: number | null;
  tick_size: string | null;
  columns: number;
  candle_interval: string;
  tiers: string[];
}

export type LongMessage =
  | LongHello
  | { type: "long_ready"; symbol: string; tier: string }
  | {
      t: "lc";
      sym: string;
      tier: string;
      bt: number;
      f: number;
      ts: number;
      s: boolean;
      b: number;
      q: Uint8Array;
      i?: Uint8Array;
      bb: number | null;
      ba: number | null;
      ll: number;
      lsh: number;
    }
  | { t: "lk"; sym: string; tier: string; iv: string; x: [number, number, number, number, number, number][] };

export interface Connection {
  /** `since`/`sinceLong`: bis hier liegen Spalten schon im Speicher (nur Rest senden) */
  subscribe(symbol: string, since?: number | null): void;
  /** Langzeit-Ansicht öffnen (Archiv + Kerzen), nur Kerzen neu laden, beenden */
  longOpen(symbol: string, tier: string): void;
  longKlines(symbol: string, tier: string): void;
  longOff(): void;
  /** Paper-Trading-Anfrage; Rückgabe = Anfrage-Nummer (kommt im `paper_result` zurück) */
  paper(op: string, payload?: Record<string, unknown>): number;
  /** Echtes Konto (Bitunix): Anfrage-Nummer kommt im `live_result` zurück */
  live(op: string, payload?: Record<string, unknown>): number;
  /** Sofort neu verbinden (z. B. nachdem ein Token eingegeben wurde) */
  reconnect(): void;
  close(): void;
}

/** Close-Code des Backends bei fehlendem/falschem Token */
export const AUTH_CLOSE_CODE = 4401;

/** `url` wird bei jedem Verbindungsaufbau neu gelesen (Token kann sich ändern). */
export function connect(url: () => string, h: Handlers, initialSymbol: () => string | null): Connection {
  let ws: WebSocket | null = null;
  let closed = false;
  let delay = 500;
  // Nach einem Reconnect die Langzeit-Ansicht neu anfordern
  let longSub: { symbol: string; tier: string } | null = null;
  let paperReq = 0;

  const send = (obj: unknown) => {
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  };

  const open = () => {
    ws = new WebSocket(url());
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      delay = 500;
      h.onConnection(true);
      const sym = initialSymbol();
      if (sym) send({ type: "subscribe", symbol: sym });
      if (longSub) send({ type: "long", ...longSub });
    };
    ws.onmessage = (ev: MessageEvent) => {
      if (typeof ev.data === "string") {
        const msg = JSON.parse(ev.data) as Hello | Status | Symbols | { type: string };
        if (msg.type === "hello") h.onHello(msg as Hello);
        else if (msg.type === "status") h.onStatus(msg as Status);
        else if (msg.type === "symbols") h.onSymbols(msg as Symbols);
        else if (msg.type === "long_hello" || msg.type === "long_ready") h.onLong(msg as LongMessage);
        else if (msg.type.startsWith("paper_")) h.onPaper?.(msg as PaperMessage);
        else if (msg.type === "live_state") h.onLive?.(msg as LiveState);
        else if (msg.type === "live_result") h.onLiveResult?.(msg as LiveResult);
      } else {
        const f = decode(new Uint8Array(ev.data as ArrayBuffer)) as BinaryFrame | Extract<LongMessage, { t: string }>;
        if (f.t === "lc" || f.t === "lk") h.onLong(f);
        else if (isChartFrame(f)) h.onChart(f);
        else h.onStat(f);
      }
    };
    ws.onclose = (ev: CloseEvent) => {
      h.onConnection(false);
      if (closed) return;
      if (ev.code === AUTH_CLOSE_CODE) {
        h.onAuthError();
        return;
      }
      setTimeout(open, delay);
      delay = Math.min(delay * 2, 10_000);
    };
  };
  open();
  return {
    subscribe: (symbol: string, since?: number | null) => {
      const msg: Record<string, unknown> = { type: "subscribe", symbol };
      if (since != null) msg.since = since;
      send(msg);
    },
    longOpen: (symbol: string, tier: string) => {
      longSub = { symbol, tier };
      send({ type: "long", symbol, tier });
    },
    longKlines: (symbol: string, tier: string) => send({ type: "long_klines", symbol, tier }),
    paper: (op: string, payload: Record<string, unknown> = {}) => {
      const req = ++paperReq;
      send({ type: "paper", op, req, ...payload });
      return req;
    },
    live: (op: string, payload: Record<string, unknown> = {}) => {
      const req = ++paperReq;
      send({ type: "live", op, req, ...payload });
      return req;
    },
    longOff: () => {
      longSub = null;
      send({ type: "long_off" });
    },
    reconnect: () => {
      if (ws && ws.readyState !== WebSocket.CLOSED) ws.close();
      else open();
    },
    close: () => {
      closed = true;
      ws?.close();
    },
  };
}
