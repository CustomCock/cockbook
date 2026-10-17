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
  /** 1 = synchron, 0 = Orderbuch nicht synchron, 2 = Lücke (keine Daten empfangen) */
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

  /**
   * `intervalMs` > 0: Zeitlücken (z. B. Backend war aus, Langzeit-Spalten aus dem Archiv,
   * Tab lange nicht offen) mit leeren Lücken-Spalten füllen. Der Puffer rechnet
   * "Spalte k = k Intervalle zurück" – ohne Füllen wären alle älteren Zeiten falsch.
   */
  constructor(
    cols: number,
    rows: number,
    readonly intervalMs = 0,
  ) {
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
   *
   * `idx` gesetzt = dünne Spalte: `values[i]` gehört zu Bucket `base + idx[i]`.
   * `center` = Bucket des Mids. Der Puffer richtet sich daran aus (nicht an der Spalte,
   * die viel breiter sein kann als der Puffer): verschoben wird erst, wenn der Mid ins
   * äußere Fünftel läuft. Werte außerhalb des Puffers fallen weg.
   */
  pushColumn(
    tsMs: number,
    synced: boolean,
    base: number,
    values: Float32Array,
    idx: Int32Array | null = null,
    center: number | null = null,
  ): number {
    if (this.count > 0 && tsMs <= this.ts[this.latest]!) return -1;
    if (this.intervalMs > 0 && this.count > 0) this.fillGap(tsMs);
    if (synced && values.length > 0) {
      const extent = idx ? (idx[idx.length - 1] ?? 0) + 1 : values.length;
      const anchor = center ?? base + extent / 2;
      if (!this.initialized) {
        this.origin = Math.round(anchor - this.rows / 2);
        this.initialized = true;
      } else if (anchor < this.origin + this.rows * 0.2 || anchor > this.origin + this.rows * 0.8) {
        this.reorigin(Math.round(anchor - this.rows / 2));
      }
    }
    const idxRow = this.head;
    const off = idxRow * this.rows;
    this.data.fill(0, off, off + this.rows);
    if (synced && values.length > 0) {
      const start = base - this.origin;
      if (idx) {
        for (let i = 0; i < values.length; i++) {
          const r = start + idx[i]!;
          if (r >= 0 && r < this.rows) this.data[off + r] = values[i]!;
        }
      } else {
        const from = Math.max(0, -start);
        const to = Math.min(values.length, this.rows - start);
        if (to > from) this.data.set(values.subarray(from, to), off + start + from);
      }
    }
    this.ts[idxRow] = tsMs;
    this.synced[idxRow] = synced ? 1 : 0;
    this.bestBid[idxRow] = NaN;
    this.bestAsk[idxRow] = NaN;
    this.trades[idxRow] = null;
    this.liqs[idxRow] = null;
    this.head = (this.head + 1) % this.cols;
    this.count = Math.min(this.count + 1, this.cols);
    this.dirty.add(idxRow);
    return idxRow;
  }

  /**
   * Wie `pushColumn`, ersetzt aber die neueste Spalte, wenn sie denselben Zeitstempel hat –
   * für die laufende Langzeit-Spalte, die bis zum Intervallende mehrmals aktualisiert wird.
   * Rückgabe: Index, `replaced` = bestehende Spalte überschrieben.
   */
  upsertColumn(
    tsMs: number,
    synced: boolean,
    base: number,
    values: Float32Array,
    idx: Int32Array | null = null,
    center: number | null = null,
  ): { index: number; replaced: boolean } {
    const replaced = this.count > 0 && this.ts[this.latest] === tsMs;
    if (replaced) {
      this.head = (this.head - 1 + this.cols) % this.cols;
      this.count--;
    }
    return { index: this.pushColumn(tsMs, synced, base, values, idx, center), replaced };
  }

  private fillGap(tsMs: number): void {
    const missing = Math.round((tsMs - this.ts[this.latest]!) / this.intervalMs) - 1;
    if (missing <= 0) return;
    const n = Math.min(missing, this.cols);
    for (let i = n; i >= 1; i--) {
      const idx = this.head;
      const off = idx * this.rows;
      this.data.fill(0, off, off + this.rows);
      this.ts[idx] = tsMs - i * this.intervalMs;
      this.synced[idx] = 2;
      this.bestBid[idx] = NaN;
      this.bestAsk[idx] = NaN;
      this.trades[idx] = null;
      this.liqs[idx] = null;
      this.head = (this.head + 1) % this.cols;
      this.count = Math.min(this.count + 1, this.cols);
      this.dirty.add(idx);
    }
    // Viele Einzel-Uploads wären langsamer als ein kompletter
    if (n > 64) {
      this.fullDirty = true;
      this.dirty.clear();
    }
  }

  /** Zeitstempel der ältesten Spalte (null = leer). */
  get oldestTs(): number | null {
    return this.count ? this.ts[this.indexAgo(this.count - 1)]! : null;
  }

  get latestTs(): number | null {
    return this.count ? this.ts[this.latest]! : null;
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

/**
 * Texturgröße: Zeilen >= 2.5x halbe Spaltenhöhe (Spalte + Platz zum Wandern),
 * Zweierpotenz, höchstens `cap` (Speicher: Spalten x Zeilen x 4 Byte).
 */
export function rowsFor(halfRange: number, maxTex: number, cap = 4096): number {
  let r = 512;
  while (r < 2.5 * halfRange && r < maxTex && r < cap) r *= 2;
  return Math.min(r, maxTex, cap);
}

/**
 * Spalte auf gröbere Preis-Buckets (`factor` feine je grobem) herunterrechnen – für die
 * zweite Preis-Ebene beim Herauszoomen. Maximum statt Summe/Mittel: Eine Wall soll auch
 * aus der Ferne so hell bleiben wie in der Nähe, und die Farbskala bleibt dieselbe.
 */
export function downsample(
  base: number,
  values: Float32Array,
  idx: Int32Array | null,
  factor: number,
): { base: number; values: Float32Array; idx: Int32Array } {
  const out = new Map<number, number>();
  for (let i = 0; i < values.length; i++) {
    const v = values[i]!;
    if (!v) continue;
    const b = Math.floor((base + (idx ? idx[i]! : i)) / factor);
    const prev = out.get(b);
    if (prev === undefined || v > prev) out.set(b, v);
  }
  const keys = [...out.keys()].sort((a, b) => a - b);
  const cb = keys[0] ?? Math.floor(base / factor);
  return {
    base: cb,
    values: Float32Array.from(keys, (k) => out.get(k)!),
    idx: Int32Array.from(keys, (k) => k - cb),
  };
}
