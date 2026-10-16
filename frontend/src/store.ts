/**
 * Ringpuffer der Heatmap-Spalten (CPU-Seite, Spiegel der GPU-Textur).
 *
 * Layout: Textur-Breite = Preis-Zeilen, Textur-Höhe = Zeit-Spalten. Damit ist eine
 * Zeit-Spalte im Speicher zusammenhängend (`data[col * rows + row]`) und kann mit
 * einem einzigen `texSubImage2D`-Aufruf (1 Texturzeile) hochgeladen werden.
 *
 * Preis-Zeilen sind absolut adressiert: Zeile r entspricht Bucket `origin + r`.
 * Läuft der Preis aus dem Bereich, wird `origin` verschoben ("Re-Origin") und alle
 * Spalten werden umkopiert – selten (Bereich ist ~4x so hoch wie eine Spalte) und
 * mit ~8 Mio. floats in wenigen ms erledigt.
 */

export interface TradeBin {
  side: 1 | -1; // +1 aggressiver Kauf, -1 aggressiver Verkauf
  vwapTicks: number;
  qty: number;
  count: number;
}

/** Liquidation: side +1 = Short liquidiert (Zwangskauf), -1 = Long liquidiert. */
export interface LiqMark {
  side: 1 | -1;
  priceTicks: number;
  qty: number;
}

export class HeatmapStore {
  readonly cols: number;
  readonly rows: number;
  readonly data: Float32Array;
  readonly ts: Float64Array;
  readonly bestBid: Float64Array; // in Ticks, NaN = unbekannt
  readonly bestAsk: Float64Array;
  readonly synced: Uint8Array;
  readonly trades: (TradeBin[] | null)[];
  readonly liqs: (LiqMark[] | null)[];
  origin = 0;
  head = 0; // nächster Schreibindex
  count = 0;
  /** Spalten, die seit dem letzten Upload geändert wurden. */
  dirty = new Set<number>();
  /** Komplette Textur neu hochladen (nach Re-Origin). */
  fullDirty = true;
  private initialized = false;

  constructor(cols: number, rows: number) {
    this.cols = cols;
    this.rows = rows;
    this.data = new Float32Array(cols * rows);
    this.ts = new Float64Array(cols);
    this.bestBid = new Float64Array(cols).fill(NaN);
    this.bestAsk = new Float64Array(cols).fill(NaN);
    this.synced = new Uint8Array(cols);
    this.trades = new Array<TradeBin[] | null>(cols).fill(null);
    this.liqs = new Array<LiqMark[] | null>(cols).fill(null);
  }

  /** Ringindex der Spalte, die `k` Spalten vor der neuesten liegt (k = 0: neueste). */
  indexAgo(k: number): number {
    return (((this.head - 1 - k) % this.cols) + this.cols) % this.cols;
  }

  get latest(): number {
    return this.indexAgo(0);
  }

  /**
   * Hängt eine Spalte an. Spalten, die nicht neuer als die letzte sind, werden
   * ignoriert (Rückgabe -1): Nach einem Reconnect schickt der Server seine Historie
   * erneut, die bereits vorhandenen Spalten dürfen nicht doppelt erscheinen.
   */
  pushColumn(tsMs: number, synced: boolean, base: number, values: Float32Array): number {
    if (this.count > 0 && tsMs <= this.ts[this.latest]!) return -1;
    if (synced && values.length > 0) {
      if (!this.initialized) {
        this.origin = Math.round(base + values.length / 2 - this.rows / 2);
        this.initialized = true;
      } else if (base < this.origin || base + values.length > this.origin + this.rows) {
        this.reorigin(Math.round(base + values.length / 2 - this.rows / 2));
      }
    }
    const idx = this.head;
    const off = idx * this.rows;
    this.data.fill(0, off, off + this.rows);
    if (synced && values.length > 0) {
      const start = base - this.origin;
      const from = Math.max(0, -start);
      const to = Math.min(values.length, this.rows - start);
      if (to > from) this.data.set(values.subarray(from, to), off + start + from);
    }
    this.ts[idx] = tsMs;
    this.synced[idx] = synced ? 1 : 0;
    this.bestBid[idx] = NaN;
    this.bestAsk[idx] = NaN;
    this.trades[idx] = null;
    this.liqs[idx] = null;
    this.head = (this.head + 1) % this.cols;
    this.count = Math.min(this.count + 1, this.cols);
    this.dirty.add(idx);
    return idx;
  }

  /** BBO/Trades gehören zur Spalte mit gleichem Zeitstempel (immer die neueste). */
  private columnFor(tsMs: number): number | null {
    if (this.count === 0) return null;
    const i = this.latest;
    return this.ts[i] === tsMs ? i : null;
  }

  setBbo(tsMs: number, bb: number | null, ba: number | null): void {
    const i = this.columnFor(tsMs);
    if (i === null) return;
    this.bestBid[i] = bb ?? NaN;
    this.bestAsk[i] = ba ?? NaN;
  }

  setTrades(tsMs: number, trades: TradeBin[]): void {
    const i = this.columnFor(tsMs);
    if (i !== null) this.trades[i] = trades;
  }

  setLiquidations(tsMs: number, liqs: LiqMark[]): void {
    const i = this.columnFor(tsMs);
    if (i !== null) this.liqs[i] = liqs;
  }

  /** Liquidität in Bucket `bucket` der Spalte `k` Spalten vor der neuesten. */
  valueAt(k: number, bucket: number): number {
    if (k < 0 || k >= this.count) return 0;
    const r = Math.floor(bucket) - this.origin;
    if (r < 0 || r >= this.rows) return 0;
    return this.data[this.indexAgo(k) * this.rows + r] ?? 0;
  }

  reorigin(newOrigin: number): void {
    const delta = newOrigin - this.origin;
    if (delta === 0) return;
    const { rows, data } = this;
    for (let c = 0; c < this.cols; c++) {
      const off = c * rows;
      if (Math.abs(delta) >= rows) {
        data.fill(0, off, off + rows);
      } else if (delta > 0) {
        data.copyWithin(off, off + delta, off + rows);
        data.fill(0, off + rows - delta, off + rows);
      } else {
        data.copyWithin(off - delta, off, off + rows + delta);
        data.fill(0, off, off - delta);
      }
    }
    this.origin = newOrigin;
    this.fullDirty = true;
    this.dirty.clear();
  }
}

/** Texturgröße: Zeilen = 4x Spaltenhöhe (Platz zum Wandern), Zweierpotenz. */
export function rowsFor(halfRange: number, maxTex: number): number {
  let r = 1024;
  while (r < 4 * halfRange && r < maxTex) r *= 2;
  return Math.min(r, maxTex);
}
