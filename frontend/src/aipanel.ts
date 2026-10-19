/**
 * KI-Panel (rechts, Knopf „KI“): Chat mit Claude oder ChatGPT, angemeldet mit deinem Account.
 *
 * Das Backend startet dafür das offizielle Programm des Anbieters (Claude Code bzw. Codex),
 * das über die CockBook-Werkzeuge Orderbuch, Trades und Kerzen liest und in den Chart zeichnet.
 * Hier liegt nur die Anzeige: Nachrichten, Werkzeugaufrufe, Eingabe. Der Verlauf selbst liegt
 * beim Anbieter-Programm; wir merken uns die Sitzungs-ID, um weiterzuschreiben.
 */

export interface AiProviderStatus {
  id: string;
  label: string;
  installed: boolean;
  ok: boolean;
  text: string;
  hint: string;
}

export type AiEvent =
  | { kind: "delta"; text: string }
  | { kind: "text"; text: string }
  | { kind: "tool"; id: string; name: string; input: unknown }
  | { kind: "tool_result"; id: string; ok: boolean; text: string }
  | { kind: "session"; id: string }
  | { kind: "error"; text: string }
  | { kind: "done"; cost_usd?: number | null; turns?: number | null };

interface Msg {
  role: "user" | "assistant" | "tool" | "error";
  text: string;
  /** Werkzeug: Name, Eingabe, Ergebnis */
  tool?: { id: string; name: string; input: string; result?: string; ok?: boolean };
}

interface Chat {
  id: string;
  session: string | null;
  msgs: Msg[];
}

export interface AiConn {
  ai(op: string, payload?: Record<string, unknown>): void;
}

const KEY = "cockbook.ai.v1";
const MAX_MSGS = 200;

const TOOL_LABEL: Record<string, string> = {
  list_symbols: "Symbole",
  get_orderbook: "Orderbuch",
  get_trade_flow: "Trade-Flow",
  get_candles: "Kerzen",
  get_market_stats: "Kennzahlen",
  get_paper_account: "Paper-Konto",
  get_live_account: "Live-Konto",
  list_drawing_tools: "Werkzeugliste",
  list_drawings: "Zeichnungen",
  add_drawing: "zeichnet",
  update_drawing: "ändert Zeichnung",
  remove_drawing: "löscht Zeichnung",
  clear_drawings: "löscht Zeichnungen",
  place_paper_order: "Paper-Order",
  close_paper_position: "Paper schließen",
  cancel_paper_orders: "Paper-Orders löschen",
};

const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

/** Sehr kleines, sicheres Markdown: nur Text-Knoten, kein innerHTML aus Modell-Ausgaben. */
export function renderMarkdown(src: string): HTMLElement {
  const root = el("div", "ai-md");
  const lines = src.replace(/\r/g, "").split("\n");
  let list: HTMLElement | null = null;
  let code: HTMLElement | null = null;
  for (const line of lines) {
    if (line.startsWith("```")) {
      if (code) code = null;
      else {
        code = el("pre", "ai-code");
        root.append(code);
      }
      continue;
    }
    if (code) {
      code.textContent += (code.textContent ? "\n" : "") + line;
      continue;
    }
    const item = /^\s*(?:[-*•]|\d+[.)])\s+(.*)$/.exec(line);
    if (item) {
      if (!list) {
        list = el(/^\s*\d/.test(line) ? "ol" : "ul");
        root.append(list);
      }
      const li = el("li");
      inline(li, item[1]!);
      list.append(li);
      continue;
    }
    list = null;
    if (!line.trim()) continue;
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    const p = el(h ? "h4" : "p");
    inline(p, h ? h[2]! : line);
    root.append(p);
  }
  return root;
}

function inline(parent: HTMLElement, text: string): void {
  // **fett**, `code` – alles andere bleibt Text
  for (const part of text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)) {
    if (!part) continue;
    if (part.startsWith("**") && part.endsWith("**")) parent.append(el("strong", "", part.slice(2, -2)));
    else if (part.startsWith("`") && part.endsWith("`")) parent.append(el("code", "", part.slice(1, -1)));
    else parent.append(document.createTextNode(part));
  }
}

function shortInput(input: unknown): string {
  if (!input || typeof input !== "object") return "";
  const o = input as Record<string, unknown>;
  const bits: string[] = [];
  for (const k of ["symbol", "tool", "text", "seconds", "minutes", "range_pct"]) {
    if (o[k] !== undefined && o[k] !== null) bits.push(String(o[k]));
  }
  return bits.join(" · ").slice(0, 60);
}

export class AiPanel {
  private provider = "claude";
  private chats: Record<string, Chat> = {};
  private status: AiProviderStatus[] = [];
  private busy = false;
  /** laufender Antwort-Text (Deltas), bis der fertige Block kommt */
  private live = "";
  private list: HTMLElement;
  private head: HTMLElement;
  private input: HTMLTextAreaElement;
  private sendBtn: HTMLButtonElement;
  private statusEl: HTMLElement;

  constructor(
    private root: HTMLElement,
    private conn: AiConn,
    private context: () => Record<string, string | number>,
    private onOpenChange: () => void,
  ) {
    this.load();
    root.classList.add("ai");
    this.head = el("div", "ai-head");
    this.statusEl = el("div", "ai-status");
    this.list = el("div", "ai-list");
    const form = el("div", "ai-form");
    this.input = el("textarea", "ai-input");
    this.input.rows = 3;
    this.input.placeholder = "Frag zum Markt … z. B. „Wo liegen die größten Wände? Zeichne sie ein.“  (Enter = senden)";
    this.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        this.send();
      }
      e.stopPropagation(); // keine Chart-Tastenkürzel beim Tippen
    });
    this.sendBtn = el("button", "ai-send", "Senden");
    this.sendBtn.type = "button";
    this.sendBtn.addEventListener("click", () => (this.busy ? this.stop() : this.send()));
    form.append(this.input, this.sendBtn);
    root.append(this.head, this.statusEl, this.list, form);
    root.hidden = !this.loadOpen();
    this.renderHead();
    this.renderList();
  }

  get open(): boolean {
    return !this.root.hidden;
  }

  toggle(): void {
    this.root.hidden = !this.root.hidden;
    try {
      localStorage.setItem(`${KEY}.open`, this.root.hidden ? "0" : "1");
    } catch {
      /* egal */
    }
    if (this.open) {
      this.conn.ai("status");
      this.input.focus();
    }
    this.onOpenChange();
  }

  /** Nach (Wieder-)Verbinden: Anmeldestatus neu holen */
  refreshStatus(): void {
    if (this.open) this.conn.ai("status");
  }

  private get chat(): Chat {
    let c = this.chats[this.provider];
    if (!c) c = this.chats[this.provider] = { id: `${this.provider}-${Date.now().toString(36)}`, session: null, msgs: [] };
    return c;
  }

  // --- Serverereignisse ----------------------------------------------------------------

  onStatus(providers: AiProviderStatus[]): void {
    this.status = providers;
    this.renderHead();
  }

  onEvent(chatId: string, ev: AiEvent): void {
    const chat = Object.values(this.chats).find((c) => c.id === chatId);
    if (!chat) return;
    switch (ev.kind) {
      case "session":
        chat.session = ev.id;
        break;
      case "delta":
        this.live += ev.text;
        break;
      case "text":
        this.live = "";
        chat.msgs.push({ role: "assistant", text: ev.text });
        break;
      case "tool":
        this.live = "";
        chat.msgs.push({ role: "tool", text: "", tool: { id: ev.id, name: ev.name, input: shortInput(ev.input) } });
        break;
      case "tool_result": {
        const m = [...chat.msgs].reverse().find((x) => x.tool?.id === ev.id);
        if (m?.tool) {
          m.tool.result = ev.text;
          m.tool.ok = ev.ok;
        }
        break;
      }
      case "error":
        chat.msgs.push({ role: "error", text: ev.text });
        break;
      case "done":
        if (this.live) chat.msgs.push({ role: "assistant", text: this.live });
        this.live = "";
        this.setBusy(false);
        break;
    }
    if (chat.msgs.length > MAX_MSGS) chat.msgs.splice(0, chat.msgs.length - MAX_MSGS);
    this.save();
    if (chat === this.chat) this.renderList();
  }

  // --- Aktionen ------------------------------------------------------------------------

  private send(): void {
    const text = this.input.value.trim();
    if (!text || this.busy) return;
    const st = this.status.find((s) => s.id === this.provider);
    if (st && !st.ok) {
      this.chat.msgs.push({ role: "error", text: `${st.label}: ${st.text}. ${st.hint}` });
      this.renderList();
      return;
    }
    const chat = this.chat;
    chat.msgs.push({ role: "user", text });
    this.input.value = "";
    this.setBusy(true);
    this.conn.ai("send", { provider: this.provider, chat: chat.id, session: chat.session, text, context: this.context() });
    this.save();
    this.renderList();
  }

  private stop(): void {
    this.conn.ai("stop", { chat: this.chat.id });
  }

  private newChat(): void {
    if (this.busy) return;
    delete this.chats[this.provider];
    this.save();
    this.renderList();
  }

  private setBusy(b: boolean): void {
    this.busy = b;
    this.sendBtn.textContent = b ? "Stopp" : "Senden";
    this.sendBtn.classList.toggle("stop", b);
  }

  // --- Anzeige -------------------------------------------------------------------------

  private renderHead(): void {
    this.head.replaceChildren();
    const tabs = el("div", "ai-tabs");
    for (const [id, label] of [
      ["claude", "Claude"],
      ["codex", "ChatGPT"],
    ] as const) {
      const st = this.status.find((s) => s.id === id);
      const b = el("button", `ai-tab${this.provider === id ? " on" : ""}`);
      b.type = "button";
      b.append(el("span", `ai-dot ${st?.ok ? "ok" : st ? "off" : ""}`), label);
      b.title = st ? `${st.label}: ${st.text}` : label;
      b.addEventListener("click", () => {
        if (this.busy) return;
        this.provider = id;
        this.save();
        this.renderHead();
        this.renderList();
      });
      tabs.append(b);
    }
    const fresh = el("button", "ai-new", "Neuer Chat");
    fresh.type = "button";
    fresh.addEventListener("click", () => this.newChat());
    this.head.append(tabs, fresh);

    const st = this.status.find((s) => s.id === this.provider);
    this.statusEl.replaceChildren();
    if (!st) {
      this.statusEl.textContent = "Status wird geprüft …";
      return;
    }
    this.statusEl.className = `ai-status${st.ok ? " ok" : " off"}`;
    this.statusEl.append(el("span", "", st.text));
    if (!st.ok) {
      this.statusEl.append(el("div", "ai-hint", st.hint));
      const again = el("button", "ai-small", "Erneut prüfen");
      again.type = "button";
      again.addEventListener("click", () => this.conn.ai("status"));
      this.statusEl.append(again);
    }
  }

  private renderList(): void {
    const l = this.list;
    l.replaceChildren();
    const msgs = this.chat.msgs;
    if (!msgs.length) {
      const empty = el("div", "ai-empty");
      empty.append(
        el("p", "", "Frag Claude oder ChatGPT direkt zu deinem Chart. Beispiele:"),
        ...[
          "Wo liegen bei diesem Coin die größten Bid- und Ask-Wände (±1 %)? Zeichne sie ein.",
          "Wie war das Delta der letzten 15 Minuten – Binance vs. Bitunix?",
          "Fasse die letzte Stunde zusammen: Trend, Volumen, auffällige Trades.",
        ].map((t) => {
          const b = el("button", "ai-example", t);
          b.type = "button";
          b.addEventListener("click", () => {
            this.input.value = t;
            this.input.focus();
          });
          return b;
        }),
        el("p", "ai-note", "Keine Anlageberatung. Echte Orders kann die KI nicht setzen."),
      );
      l.append(empty);
    }
    for (const m of msgs) {
      if (m.role === "user") l.append(el("div", "ai-msg user", m.text));
      else if (m.role === "assistant") {
        const d = el("div", "ai-msg bot");
        d.append(renderMarkdown(m.text));
        l.append(d);
      } else if (m.role === "error") l.append(el("div", "ai-msg err", m.text));
      else if (m.tool) {
        const t = m.tool;
        const d = el("details", `ai-tool${t.ok === false ? " bad" : ""}`);
        const s = el("summary");
        s.append(el("span", "ai-tool-ico", t.result === undefined ? "⋯" : t.ok === false ? "✕" : "✓"), `${TOOL_LABEL[t.name] ?? t.name}`);
        if (t.input) s.append(el("span", "ai-tool-in", t.input));
        d.append(s);
        if (t.result) d.append(el("pre", "ai-code", t.result));
        l.append(d);
      }
    }
    if (this.live) {
      const d = el("div", "ai-msg bot");
      d.append(renderMarkdown(this.live));
      l.append(d);
    }
    if (this.busy) l.append(el("div", "ai-typing", "…"));
    l.scrollTop = l.scrollHeight;
  }

  // --- Speicher (nur dieser Browser) -------------------------------------------------

  private load(): void {
    try {
      const raw = localStorage.getItem(KEY);
      if (raw) {
        const s = JSON.parse(raw) as { provider?: string; chats?: Record<string, Chat> };
        if (s.provider === "claude" || s.provider === "codex") this.provider = s.provider;
        this.chats = s.chats ?? {};
      }
    } catch {
      /* privates Fenster o. Ä. */
    }
  }

  private loadOpen(): boolean {
    try {
      return localStorage.getItem(`${KEY}.open`) === "1";
    } catch {
      return false;
    }
  }

  private save(): void {
    try {
      localStorage.setItem(KEY, JSON.stringify({ provider: this.provider, chats: this.chats }));
    } catch {
      /* voll o. Ä. */
    }
  }
}
