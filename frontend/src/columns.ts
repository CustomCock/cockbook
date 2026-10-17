/**
 * Setzt Heatmap-Spalten aus Schlüsselbildern + Änderungen wieder zusammen.
 *
 * Der Server schickt alle paar Sekunden eine vollständige Spalte (`k` = true), dazwischen
 * nur geänderte Buckets (Wert 0 = Bucket leer). `n` zählt je Symbol lückenlos hoch. Fehlt
 * eine Nummer (langsamer Tab, Server hat Frames verworfen), ist der Zustand unbekannt:
 * Bis zum nächsten Schlüsselbild liefert `apply` dann `null` – lieber kurz "keine Daten"
 * zeigen als still eine falsche Heatmap.
 */

export interface SparseColumn {
  base: number;
  values: Float32Array;
  idx: Int32Array;
}

export class ColumnAssembler {
  private state = new Map<number, number>();
  private lastSeq: number | null = null;

  /** Zustand verwerfen (z. B. neue Skalierung nach `hello`). */
  reset(): void {
    this.state.clear();
    this.lastSeq = null;
  }

  /**
   * `synced` = false: Buch nicht synchron, Zustand weg. Rückgabe: vollständige dünne
   * Spalte, oder `null` wenn sie unbekannt ist (unsynchron oder Lücke vor einem Schlüsselbild).
   * Ohne `k`/`n` (älterer Server) ist jede Spalte vollständig.
   */
  apply(
    synced: boolean,
    keyframe: boolean | undefined,
    seq: number | undefined,
    base: number,
    values: Float32Array,
    idx: Int32Array | null,
  ): SparseColumn | null {
    const prev = this.lastSeq;
    this.lastSeq = seq ?? null;
    if (!synced) {
      this.state.clear();
      this.lastSeq = null; // danach kommt immer ein Schlüsselbild
      return null;
    }
    const full = keyframe ?? true;
    if (!full && (prev === null || seq === undefined || seq !== prev + 1)) {
      this.state.clear();
      this.lastSeq = null; // bis zum nächsten Schlüsselbild nichts annehmen
      return null;
    }
    const s = this.state;
    if (full) s.clear();
    for (let i = 0; i < values.length; i++) {
      const b = base + (idx ? idx[i]! : i);
      const v = values[i]!;
      if (v) s.set(b, v);
      else s.delete(b);
    }
    return this.snapshot();
  }

  private snapshot(): SparseColumn {
    const keys = Int32Array.from(this.state.keys()).sort();
    const base = keys.length ? keys[0]! : 0;
    const values = new Float32Array(keys.length);
    const idx = new Int32Array(keys.length);
    for (let i = 0; i < keys.length; i++) {
      values[i] = this.state.get(keys[i]!)!;
      idx[i] = keys[i]! - base;
    }
    return { base, values, idx };
  }
}
