import { describe, expect, it } from "vitest";
import { ColumnAssembler } from "../src/columns";

const f = (xs: number[]) => Float32Array.from(xs);
const i = (xs: number[]) => Int32Array.from(xs);

describe("ColumnAssembler", () => {
  it("baut aus Schlüsselbild + Änderungen die volle Spalte", () => {
    const a = new ColumnAssembler();
    let c = a.apply(true, true, 1, 10, f([1, 2, 3]), i([0, 2, 5]));
    expect(c && [c.base, [...c.idx], [...c.values]]).toEqual([10, [0, 2, 5], [1, 2, 3]]);
    // Bucket 12 weg, 8 neu, 15 geändert
    c = a.apply(true, false, 2, 8, f([7, 0, 9]), i([0, 4, 7]));
    expect(c && [c.base, [...c.idx], [...c.values]]).toEqual([8, [0, 2, 7], [7, 1, 9]]);
  });

  it("wartet nach einer Lücke auf das nächste Schlüsselbild", () => {
    const a = new ColumnAssembler();
    a.apply(true, true, 1, 0, f([1]), i([0]));
    expect(a.apply(true, false, 3, 0, f([2]), i([0]))).toBeNull(); // 2 fehlt
    expect(a.apply(true, false, 4, 0, f([2]), i([0]))).toBeNull(); // immer noch unbekannt
    const c = a.apply(true, true, 5, 4, f([5]), i([0]));
    expect(c && [...c.values]).toEqual([5]);
  });

  it("verwirft den Zustand bei unsynchronem Buch", () => {
    const a = new ColumnAssembler();
    a.apply(true, true, 1, 0, f([1]), i([0]));
    expect(a.apply(false, true, 2, 0, f([]), i([]))).toBeNull();
    expect(a.apply(true, false, 3, 0, f([2]), i([0]))).toBeNull();
  });

  it("nimmt Spalten ohne k/n als vollständig (dicht)", () => {
    const a = new ColumnAssembler();
    const c = a.apply(true, undefined, undefined, 3, f([1, 0, 2]), null);
    expect(c && [c.base, [...c.idx], [...c.values]]).toEqual([3, [0, 2], [1, 2]]);
  });
});
