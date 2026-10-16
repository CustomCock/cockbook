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
  half_range_buckets: number;
  ui: { recenter_margin: number; bubble_px_per_sqrt_kusd: number; bubble_max_px: number };
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
}

export interface Symbols {
  type: "symbols";
  symbols: SymbolInfo[];
}

/** Chart-Frames (nur für das abonnierte Symbol). */
export type ChartFrame =
  | { t: "col"; sym: string; ts: number; s: boolean; b: number; q: Uint8Array }
  | { t: "bbo"; sym: string; ts: number; bb: number | null; ba: number | null }
  | { t: "tr"; sym: string; ts: number; x: [number, number, number, number][] }
  | { t: "lq"; sym: string; ts: number; x: [number, number, number][] };

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
  | { t: "liq"; sym: string; ts: number; side: 1 | -1; price: number; qty: number };

export type BinaryFrame = ChartFrame | StatFrame;

export function isChartFrame(f: BinaryFrame): f is ChartFrame {
  return f.t === "col" || f.t === "bbo" || f.t === "tr" || f.t === "lq";
}

export interface Handlers {
  onSymbols(s: Symbols): void;
  onHello(h: Hello): void;
  onStatus(s: Status): void;
  onChart(f: ChartFrame): void;
  onStat(f: StatFrame): void;
  onConnection(connected: boolean): void;
}

/** float32-LE-Bytes -> Float32Array. Kopie nötig: msgpack liefert eine Sicht mit
 *  beliebigem Byte-Offset, Float32Array braucht 4-Byte-Ausrichtung. */
export function toFloat32(bytes: Uint8Array): Float32Array {
  return new Float32Array(bytes.slice().buffer);
}

export interface Connection {
  subscribe(symbol: string): void;
  close(): void;
}

export function connect(url: string, h: Handlers, initialSymbol: () => string | null): Connection {
  let ws: WebSocket | null = null;
  let closed = false;
  let delay = 500;

  const send = (obj: unknown) => {
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  };

  const open = () => {
    ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      delay = 500;
      h.onConnection(true);
      const sym = initialSymbol();
      if (sym) send({ type: "subscribe", symbol: sym });
    };
    ws.onmessage = (ev: MessageEvent) => {
      if (typeof ev.data === "string") {
        const msg = JSON.parse(ev.data) as Hello | Status | Symbols | { type: string };
        if (msg.type === "hello") h.onHello(msg as Hello);
        else if (msg.type === "status") h.onStatus(msg as Status);
        else if (msg.type === "symbols") h.onSymbols(msg as Symbols);
      } else {
        const f = decode(new Uint8Array(ev.data as ArrayBuffer)) as BinaryFrame;
        if (isChartFrame(f)) h.onChart(f);
        else h.onStat(f);
      }
    };
    ws.onclose = () => {
      h.onConnection(false);
      if (closed) return;
      setTimeout(open, delay);
      delay = Math.min(delay * 2, 10_000);
    };
  };
  open();
  return {
    subscribe: (symbol: string) => send({ type: "subscribe", symbol }),
    close: () => {
      closed = true;
      ws?.close();
    },
  };
}
