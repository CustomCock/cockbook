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
out vec4 outColor;

float sampleMax(int k0, int nk, int r0, int nr) {
  float m = 0.0;
  for (int i = 0; i < 4; i++) {
    if (i >= nk) break;
    int k = k0 + i;
    if (k < 0 || k >= uCount) continue;
    int col = ((uHead - k) % uCols + uCols) % uCols;
    for (int j = 0; j < 4; j++) {
      if (j >= nr) break;
      int r = r0 + j;
      if (r < 0 || r >= uRows) continue;
      m = max(m, texelFetch(uData, ivec2(r, col), 0).r);
    }
  }
  return m;
}

void main() {
  vec2 frag = gl_FragCoord.xy;               // Ursprung unten links
  float fromRight = uSize.x - frag.x;
  int k0 = int(floor(fromRight / uColW + uOffset));
  float unitsPerPx = (uYHi - uYLo) / uSize.y;
  float u = uYLo + frag.y * unitsPerPx;
  int r0 = int(floor(u));
  // Herausgezoomt: mehrere Spalten/Zeilen pro Pixel -> Maximum nehmen, damit
  // einzelne Walls nicht durch Aliasing verschwinden (auf 4x4 begrenzt).
  int nk = clamp(int(ceil(1.0 / uColW)), 1, 4);
  int nr = clamp(int(ceil(unitsPerPx)), 1, 4);
  float v;
  if (nk == 1 && nr == 1) {
    if (k0 < 0 || k0 >= uCount || r0 < 0 || r0 >= uRows) { v = 0.0; }
    else { int col = uHead - k0; if (col < 0) col += uCols; v = texelFetch(uData, ivec2(r0, col), 0).r; }
  } else {
    v = sampleMax(k0, nk, r0, nr);
  }
  if (v <= uThreshold || v <= 0.0) {
    outColor = vec4(uBg, 1.0);
    return;
  }
  float t = clamp((log(v) - uLogLo) / (uLogHi - uLogLo), 0.0, 1.0);
  outColor = texture(uLut, vec2(t * (255.0 / 256.0) + 0.5 / 256.0, 0.5));
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
      "uYLo", "uYHi", "uThreshold", "uLogLo", "uLogHi", "uBg",
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
  draw(view: View, range: ScaleRange | null, pxScale: number): void {
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
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
}

// ======================================================================================
// Primitive: Linien, Dreiecke, Bubbles (Point-Sprites) über der Heatmap
// ======================================================================================

const PRIM_VS = `#version 300 es
layout(location = 0) in vec2 aPos;     // CSS-px, Ursprung oben links
layout(location = 1) in float aRadius; // CSS-px (nur Punkte)
layout(location = 2) in vec4 aColor;
uniform vec2 uRes;                     // Plotgröße in CSS-px
uniform float uPx;                     // Device-px pro CSS-px
uniform float uMaxPoint;
out vec4 vColor;
out float vSize;
void main() {
  vec2 clip = vec2(aPos.x / uRes.x * 2.0 - 1.0, 1.0 - aPos.y / uRes.y * 2.0);
  gl_Position = vec4(clip, 0.0, 1.0);
  vSize = min(aRadius * 2.0 * uPx + 2.0, uMaxPoint);
  gl_PointSize = vSize;
  vColor = aColor;
}`;

const PRIM_FS = `#version 300 es
precision mediump float;
uniform bool uPoints;
in vec4 vColor;
in float vSize;
out vec4 outColor;
void main() {
  if (!uPoints) { outColor = vColor; return; }
  // Kreis mit kräftigerem Rand
  float d = length(gl_PointCoord * 2.0 - 1.0);
  if (d > 1.0) discard;
  float ring = 1.0 - 3.0 / vSize;
  float a = d > ring ? min(1.0, vColor.a + 0.4) : vColor.a;
  outColor = vec4(vColor.rgb, a);
}`;

export class PrimRenderer {
  private prog: WebGLProgram;
  private vao: WebGLVertexArrayObject;
  private buf: WebGLBuffer;
  private uRes: WebGLUniformLocation | null;
  private uPx: WebGLUniformLocation | null;
  private uPoints: WebGLUniformLocation | null;
  private uMaxPoint: WebGLUniformLocation | null;
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
    this.uRes = gl.getUniformLocation(prog, "uRes");
    this.uPx = gl.getUniformLocation(prog, "uPx");
    this.uPoints = gl.getUniformLocation(prog, "uPoints");
    this.uMaxPoint = gl.getUniformLocation(prog, "uMaxPoint");
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

  /** mode: gl.LINES | gl.TRIANGLES | gl.POINTS */
  draw(mode: number, data: Float32Array, vertices: number, cssW: number, cssH: number, px: number): void {
    if (vertices === 0) return;
    const gl = this.gl;
    gl.useProgram(this.prog);
    gl.bindVertexArray(this.vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
    gl.bufferData(gl.ARRAY_BUFFER, data.subarray(0, vertices * 7), gl.STREAM_DRAW);
    gl.uniform2f(this.uRes, cssW, cssH);
    gl.uniform1f(this.uPx, px);
    gl.uniform1i(this.uPoints, mode === gl.POINTS ? 1 : 0);
    gl.uniform1f(this.uMaxPoint, this.maxPoint);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.drawArrays(mode, 0, vertices);
    gl.disable(gl.BLEND);
    gl.bindVertexArray(null);
  }
}
