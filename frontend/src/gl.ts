/**
 * WebGL2-Heatmap-Renderer.
 *
 * Die komplette Historie liegt als R32F-Textur auf der GPU (Ringpuffer, siehe
 * store.ts). Pro neuer Spalte wird nur eine Texturzeile hochgeladen; gezeichnet wird
 * ein einziges bildschirmfüllendes Dreieck, der Fragment-Shader rechnet pro Pixel aus,
 * welche Spalte/Zeile er zeigt. Kosten pro Frame sind damit unabhängig davon, wie
 * viele Spalten sichtbar sind – Canvas2D mit fillRect pro Zelle wäre bei
 * 1500 x 800 Zellen ~1,2 Mio. Draw-Calls pro Frame.
 *
 * `texelFetch` statt `texture()`: exakte Ganzzahl-Adressierung, kein Filtern über
 * Spalten-/Zeilengrenzen und keine Abhängigkeit von OES_texture_float_linear.
 */

import { buildLut, BACKGROUND, type ScaleRange } from "./colorscale";
import type { HeatmapStore } from "./store";
import type { View } from "./viewport";

const VS = `#version 300 es
void main() {
  // Ein Dreieck, das den ganzen Viewport abdeckt
  vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`;

const FS = `#version 300 es
precision highp float;
precision highp int;
precision highp sampler2D;

uniform sampler2D uData;   // R32F: x = Preis-Zeile, y = Zeit-Spalte (Ring)
uniform sampler2D uLut;    // RGBA8 256x1
uniform vec2 uSize;        // Plotgröße in Device-Pixeln
uniform float uColW;       // Device-Pixel pro Spalte
uniform float uOffset;     // zurückgescrollte Spalten
uniform int uHead;         // Ringindex der neuesten Spalte
uniform int uCount;        // gültige Spalten
uniform int uCols;
uniform int uRows;
uniform float uYLo;        // relativ zu origin, in Buckets
uniform float uYHi;
uniform float uThreshold;
uniform float uLogLo;
uniform float uLogHi;
uniform vec3 uBg;
uniform bool uSmooth;      // weich interpolieren statt harter Zellen
uniform int uTaps;         // vertikale Glättung: 1 = nur bilinear, 3 = zusätzlich ±2 px
out vec4 outColor;

float fetch(int k, int r) {
  if (k < 0 || k >= uCount || r < 0 || r >= uRows) return 0.0;
  int col = uHead - k;
  if (col < 0) col += uCols;
  return texelFetch(uData, ivec2(r, col), 0).r;
}

float sampleMax(int k0, int nk, int r0, int nr, int kStep) {
  float m = 0.0;
  for (int i = 0; i < 4; i++) {
    if (i >= nk) break;
    for (int j = 0; j < 4; j++) {
      if (j >= nr) break;
      m = max(m, fetch(k0 + i * kStep, r0 + j));
    }
  }
  return m;
}

void main() {
  vec2 frag = gl_FragCoord.xy;               // Ursprung unten links
  float cf = (uSize.x - frag.x) / uColW + uOffset;   // Spalten ab der neuesten
  float unitsPerPx = (uYHi - uYLo) / uSize.y;
  float u = uYLo + frag.y * unitsPerPx;               // Preis-Zeile (kontinuierlich)
  // Herausgezoomt: mehrere Spalten/Zeilen pro Pixel -> Maximum nehmen, damit
  // einzelne Walls nicht durch Aliasing verschwinden (auf 4x4 begrenzt).
  int nk = clamp(int(ceil(1.0 / uColW)), 1, 4);
  int nr = clamp(int(ceil(unitsPerPx)), 1, 4);
  float v;
  if (nk > 1 || nr > 1) {
    // >4 Spalten pro Pixel: 4 Stichproben gleichmäßig über den Pixel verteilt
    int kStep = max(1, int(ceil(1.0 / uColW)) / 4);
    v = sampleMax(int(floor(cf)), nk, int(floor(u)), nr, kStep);
  } else if (uSmooth) {
    // Weich: bilinear zwischen Zellmitten (wirkt, wenn Zeilen groß sind) plus leichte
    // vertikale Glättung über ~±2 Bildschirm-Pixel (wirkt, wenn Zeilen klein sind).
    float kc = cf - 0.5;
    int k0 = int(floor(kc));
    float fk = smoothstep(0.0, 1.0, fract(kc));
    float spread = 1.8 * unitsPerPx;
    v = 0.0;
    int h = uTaps / 2;
    for (int i = -1; i <= 1; i++) {
      if (i < -h || i > h) continue;
      float rc = u + float(i) * spread - 0.5;
      int r0 = int(floor(rc));
      float fr = smoothstep(0.0, 1.0, fract(rc));
      float a = mix(fetch(k0, r0), fetch(k0, r0 + 1), fr);
      float b = mix(fetch(k0 + 1, r0), fetch(k0 + 1, r0 + 1), fr);
      v += mix(a, b, fk) * (h == 0 ? 1.0 : (i == 0 ? 0.5 : 0.25));
    }
  } else {
    v = fetch(int(floor(cf)), int(floor(u)));
  }
  if (v <= 0.0) {
    outColor = vec4(uBg, 1.0);
    return;
  }
  float t = clamp((log(v) - uLogLo) / (uLogHi - uLogLo), 0.0, 1.0);
  vec3 c = texture(uLut, vec2(t * (255.0 / 256.0) + 0.5 / 256.0, 0.5)).rgb;
  // Schwelle weich ausblenden statt hart abschneiden
  float alpha = uThreshold > 0.0 ? smoothstep(uThreshold * 0.6, uThreshold, v) : 1.0;
  outColor = vec4(mix(uBg, c, alpha), 1.0);
}`;

function compile(gl: WebGL2RenderingContext, type: number, src: string): WebGLShader {
  const s = gl.createShader(type)!;
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
    throw new Error(`Shader-Fehler: ${gl.getShaderInfoLog(s)}`);
  }
  return s;
}

export class HeatmapRenderer {
  readonly gl: WebGL2RenderingContext;
  private prog: WebGLProgram;
  private dataTex: WebGLTexture;
  private lutTex: WebGLTexture;
  private u: Record<string, WebGLUniformLocation | null> = {};
  private store: HeatmapStore | null = null;
  readonly maxTextureSize: number;
  /** Name des WebGL-Renderers, z. B. "ANGLE (… SwiftShader …)" oder die echte GPU. */
  readonly rendererName: string;
  /** Software-Rasterizer (keine GPU, z. B. VM ohne GPU-Durchreichung). */
  readonly isSoftware: boolean;

  constructor(readonly canvas: HTMLCanvasElement) {
    const gl = canvas.getContext("webgl2", { antialias: false, premultipliedAlpha: false });
    if (!gl) throw new Error("WebGL2 wird von diesem Browser nicht unterstützt");
    this.gl = gl;
    this.maxTextureSize = gl.getParameter(gl.MAX_TEXTURE_SIZE) as number;
    const dbg = gl.getExtension("WEBGL_debug_renderer_info");
    this.rendererName = String(
      dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
    );
    this.isSoftware = /swiftshader|llvmpipe|softpipe|software/i.test(this.rendererName);
    const prog = gl.createProgram()!;
    gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VS));
    gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      throw new Error(`Link-Fehler: ${gl.getProgramInfoLog(prog)}`);
    }
    this.prog = prog;
    for (const name of [
      "uData", "uLut", "uSize", "uColW", "uOffset", "uHead", "uCount", "uCols", "uRows",
      "uYLo", "uYHi", "uThreshold", "uLogLo", "uLogHi", "uBg", "uSmooth", "uTaps",
    ]) {
      this.u[name] = gl.getUniformLocation(prog, name);
    }
    this.dataTex = gl.createTexture()!;
    this.lutTex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, this.lutTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, buildLut());
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
  }

  attach(store: HeatmapStore): void {
    const gl = this.gl;
    this.store = store;
    gl.bindTexture(gl.TEXTURE_2D, this.dataTex);
    gl.texStorage2D(gl.TEXTURE_2D, 1, gl.R32F, store.rows, store.cols);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    store.fullDirty = true;
  }

  /** Lädt geänderte Spalten hoch. Gibt die Anzahl hochgeladener Spalten zurück. */
  upload(): number {
    const s = this.store;
    if (!s) return 0;
    const gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D, this.dataTex);
    if (s.fullDirty) {
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, s.rows, s.cols, gl.RED, gl.FLOAT, s.data);
      s.fullDirty = false;
      s.dirty.clear();
      return s.cols;
    }
    const n = s.dirty.size;
    for (const c of s.dirty) {
      gl.texSubImage2D(
        gl.TEXTURE_2D, 0, 0, c, s.rows, 1, gl.RED, gl.FLOAT,
        s.data, c * s.rows,
      );
    }
    s.dirty.clear();
    return n;
  }

  /** `pxScale` = Device-Pixel pro CSS-Pixel *dieses* Canvas (dpr x Render-Skala). */
  draw(view: View, range: ScaleRange | null, pxScale: number, smooth = true): void {
    const gl = this.gl;
    const s = this.store;
    const w = this.canvas.width;
    const h = this.canvas.height;
    gl.viewport(0, 0, w, h);
    const bg = BACKGROUND.map((c) => c / 255) as [number, number, number];
    if (!s || s.count === 0 || !range) {
      gl.clearColor(bg[0], bg[1], bg[2], 1);
      gl.clear(gl.COLOR_BUFFER_BIT);
      return;
    }
    gl.useProgram(this.prog);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.dataTex);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.lutTex);
    const u = this.u;
    gl.uniform1i(u.uData!, 0);
    gl.uniform1i(u.uLut!, 1);
    gl.uniform2f(u.uSize!, w, h);
    gl.uniform1f(u.uColW!, view.colWidth * pxScale);
    gl.uniform1f(u.uOffset!, view.offset);
    gl.uniform1i(u.uHead!, s.latest);
    gl.uniform1i(u.uCount!, s.count);
    gl.uniform1i(u.uCols!, s.cols);
    gl.uniform1i(u.uRows!, s.rows);
    // relativ zu origin -> kleine Zahlen, keine float32-Präzisionsprobleme
    gl.uniform1f(u.uYLo!, view.yLo - s.origin);
    gl.uniform1f(u.uYHi!, view.yHi - s.origin);
    gl.uniform1f(u.uThreshold!, range.threshold);
    gl.uniform1f(u.uLogLo!, range.logLo);
    gl.uniform1f(u.uLogHi!, range.logHi);
    gl.uniform3f(u.uBg!, bg[0], bg[1], bg[2]);
    gl.uniform1i(u.uSmooth!, smooth ? 1 : 0);
    // Software-Renderer: jeder Texturzugriff kostet CPU -> nur bilinear; das
    // Hochskalieren der halben Auflösung durch den Browser glättet zusätzlich.
    gl.uniform1i(u.uTaps!, this.isSoftware ? 1 : 3);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
}

// ======================================================================================
// Primitive: Linien, Dreiecke, Bubbles (Point-Sprites) über der Heatmap
// ======================================================================================

const PRIM_VS = `#version 300 es
layout(location = 0) in vec2 aPos;     // CSS-px, Ursprung oben links
layout(location = 1) in float aAux;    // Punkte: Radius (CSS-px); weiche Linien: Abstand zur Mitte
layout(location = 2) in vec4 aColor;
uniform vec2 uRes;                     // Plotgröße in CSS-px
uniform float uPx;                     // Device-px pro CSS-px
uniform float uMaxPoint;
out vec4 vColor;
out float vSize;
out float vAux;
void main() {
  vec2 clip = vec2(aPos.x / uRes.x * 2.0 - 1.0, 1.0 - aPos.y / uRes.y * 2.0);
  gl_Position = vec4(clip, 0.0, 1.0);
  vSize = min(aAux * 2.0 * uPx + 2.0, uMaxPoint);
  gl_PointSize = vSize;
  vAux = aAux;
  vColor = aColor;
}`;

const PRIM_FS = `#version 300 es
precision mediump float;
uniform int uMode;      // 0 = Flächen, 1 = Kreis, 2 = Raute, 3 = weiche Linie
uniform float uHalf;    // weiche Linie: halbe Kernbreite (CSS-px)
uniform float uFeather; // weiche Linie: Auslaufbreite (CSS-px)
in vec4 vColor;
in float vSize;
in float vAux;
out vec4 outColor;
void main() {
  if (uMode == 0) { outColor = vColor; return; }
  if (uMode == 3) {
    float a = 1.0 - smoothstep(uHalf, uHalf + uFeather, abs(vAux));
    outColor = vec4(vColor.rgb, vColor.a * a);
    return;
  }
  vec2 p = gl_PointCoord * 2.0 - 1.0;
  // Kreis bzw. Raute (L1-Norm); Rand weich über ~1,5 px
  float d = uMode == 2 ? abs(p.x) + abs(p.y) : length(p);
  float px = 3.0 / vSize;
  float edge = 1.0 - smoothstep(1.0 - px, 1.0, d);
  if (edge <= 0.0) discard;
  float ring = smoothstep(1.0 - 3.0 * px, 1.0 - px, d);
  float a = mix(vColor.a, min(1.0, vColor.a + 0.35), ring) * edge;
  outColor = vec4(vColor.rgb, a);
}`;

export const PRIM = { FLAT: 0, CIRCLE: 1, DIAMOND: 2, SOFT_LINE: 3 } as const;

export class PrimRenderer {
  private prog: WebGLProgram;
  private vao: WebGLVertexArrayObject;
  private buf: WebGLBuffer;
  private u: Record<string, WebGLUniformLocation | null> = {};
  private maxPoint: number;

  constructor(private gl: WebGL2RenderingContext) {
    const prog = gl.createProgram()!;
    gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, PRIM_VS));
    gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, PRIM_FS));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      throw new Error(`Link-Fehler: ${gl.getProgramInfoLog(prog)}`);
    }
    this.prog = prog;
    for (const n of ["uRes", "uPx", "uMaxPoint", "uMode", "uHalf", "uFeather"]) {
      this.u[n] = gl.getUniformLocation(prog, n);
    }
    const range = gl.getParameter(gl.ALIASED_POINT_SIZE_RANGE) as Float32Array;
    this.maxPoint = range[1] ?? 64;
    this.vao = gl.createVertexArray()!;
    this.buf = gl.createBuffer()!;
    gl.bindVertexArray(this.vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
    const stride = 7 * 4;
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, stride, 0);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 1, gl.FLOAT, false, stride, 8);
    gl.enableVertexAttribArray(2);
    gl.vertexAttribPointer(2, 4, gl.FLOAT, false, stride, 12);
    gl.bindVertexArray(null);
  }

  /**
   * mode: PRIM.FLAT/SOFT_LINE -> Dreiecke, PRIM.CIRCLE/DIAMOND -> Punkte.
   * Weiche Linien: `half`/`feather` bestimmen Kern- und Auslaufbreite.
   */
  draw(
    mode: number,
    data: Float32Array,
    vertices: number,
    cssW: number,
    cssH: number,
    px: number,
    half = 0.5,
    feather = 1,
  ): void {
    if (vertices === 0) return;
    const gl = this.gl;
    const u = this.u;
    gl.useProgram(this.prog);
    gl.bindVertexArray(this.vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
    gl.bufferData(gl.ARRAY_BUFFER, data.subarray(0, vertices * 7), gl.STREAM_DRAW);
    gl.uniform2f(u.uRes!, cssW, cssH);
    gl.uniform1f(u.uPx!, px);
    gl.uniform1i(u.uMode!, mode);
    gl.uniform1f(u.uHalf!, half);
    gl.uniform1f(u.uFeather!, feather);
    gl.uniform1f(u.uMaxPoint!, this.maxPoint);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    const isPoint = mode === PRIM.CIRCLE || mode === PRIM.DIAMOND;
    gl.drawArrays(isPoint ? gl.POINTS : gl.TRIANGLES, 0, vertices);
    gl.disable(gl.BLEND);
    gl.bindVertexArray(null);
  }
}

// ======================================================================================
// Abgerundete Rechtecke (Kerzen) per Signed Distance Field
// ======================================================================================

const RRECT_VS = `#version 300 es
layout(location = 0) in vec2 aPos;    // CSS-px
layout(location = 1) in vec2 aLocal;  // Position relativ zur Rechteckmitte (CSS-px)
layout(location = 2) in vec2 aHalf;   // halbe Größe (CSS-px)
layout(location = 3) in float aRadius;
layout(location = 4) in vec4 aColor;
uniform vec2 uRes;
out vec2 vLocal;
out vec2 vHalf;
out float vRadius;
out vec4 vColor;
void main() {
  gl_Position = vec4(aPos.x / uRes.x * 2.0 - 1.0, 1.0 - aPos.y / uRes.y * 2.0, 0.0, 1.0);
  vLocal = aLocal;
  vHalf = aHalf;
  vRadius = aRadius;
  vColor = aColor;
}`;

const RRECT_FS = `#version 300 es
precision mediump float;
uniform float uPx;   // Device-px pro CSS-px (für eine 1-Pixel-breite weiche Kante)
in vec2 vLocal;
in vec2 vHalf;
in float vRadius;
in vec4 vColor;
out vec4 outColor;
void main() {
  // Abstand zur abgerundeten Kontur: < 0 innen, > 0 außen
  vec2 q = abs(vLocal) - vHalf + vRadius;
  float d = length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - vRadius;
  float aa = 0.75 / uPx;
  float a = 1.0 - smoothstep(-aa, aa, d);
  if (a <= 0.0) discard;
  outColor = vec4(vColor.rgb, vColor.a * a);
}`;

export class RRectRenderer {
  private prog: WebGLProgram;
  private vao: WebGLVertexArrayObject;
  private buf: WebGLBuffer;
  private uRes: WebGLUniformLocation | null;
  private uPx: WebGLUniformLocation | null;

  constructor(private gl: WebGL2RenderingContext) {
    const prog = gl.createProgram()!;
    gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, RRECT_VS));
    gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, RRECT_FS));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      throw new Error(`Link-Fehler: ${gl.getProgramInfoLog(prog)}`);
    }
    this.prog = prog;
    this.uRes = gl.getUniformLocation(prog, "uRes");
    this.uPx = gl.getUniformLocation(prog, "uPx");
    this.vao = gl.createVertexArray()!;
    this.buf = gl.createBuffer()!;
    gl.bindVertexArray(this.vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
    const stride = 11 * 4;
    const attrs: [number, number, number][] = [
      [0, 2, 0],
      [1, 2, 8],
      [2, 2, 16],
      [3, 1, 24],
      [4, 4, 28],
    ];
    for (const [loc, size, off] of attrs) {
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, size, gl.FLOAT, false, stride, off);
    }
    gl.bindVertexArray(null);
  }

  draw(data: Float32Array, vertices: number, cssW: number, cssH: number, px: number): void {
    if (vertices === 0) return;
    const gl = this.gl;
    gl.useProgram(this.prog);
    gl.bindVertexArray(this.vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
    gl.bufferData(gl.ARRAY_BUFFER, data.subarray(0, vertices * 11), gl.STREAM_DRAW);
    gl.uniform2f(this.uRes, cssW, cssH);
    gl.uniform1f(this.uPx, px);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.drawArrays(gl.TRIANGLES, 0, vertices);
    gl.disable(gl.BLEND);
    gl.bindVertexArray(null);
  }
}
