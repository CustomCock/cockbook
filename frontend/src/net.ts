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
  ui: { recenter_margin: number; bubble_px_per_unit: number; bubble_max_px: number };
}

export interface Status {
  type: "status";
  synced: boolean;
  reason: string;
}

export type BinaryFrame =
  | { t: "col"; ts: number; s: boolean; b: number; q: Uint8Array }
  | { t: "bbo"; ts: number; bb: number | null; ba: number | null }
  | { t: "tr"; ts: number; x: [number, number, number, number][] };

export interface Handlers {
  onHello(h: Hello): void;
  onStatus(s: Status): void;
  onFrame(f: BinaryFrame): void;
  onConnection(connected: boolean): void;
}

/** float32-LE-Bytes -> Float32Array. Kopie nötig: msgpack liefert eine Sicht mit
 *  beliebigem Byte-Offset, Float32Array braucht 4-Byte-Ausrichtung. */
export function toFloat32(bytes: Uint8Array): Float32Array {
  return new Float32Array(bytes.slice().buffer);
}

export function connect(url: string, h: Handlers): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let delay = 500;

  const open = () => {
    ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      delay = 500;
      h.onConnection(true);
    };
    ws.onmessage = (ev: MessageEvent) => {
      if (typeof ev.data === "string") {
        const msg = JSON.parse(ev.data) as Hello | Status;
        if (msg.type === "hello") h.onHello(msg);
        else if (msg.type === "status") h.onStatus(msg);
      } else {
        h.onFrame(decode(new Uint8Array(ev.data as ArrayBuffer)) as BinaryFrame);
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
  return () => {
    closed = true;
    ws?.close();
  };
}
