/**
 * Handels-Panel (rechts): Ordermaske, Position, offene Orders, Verlauf – Paper-Trading.
 *
 * Das Panel schickt nur Anfragen (`conn.paper(...)`); was wirklich passiert ist, kommt als
 * `paper_state` vom Backend zurück und wird angezeigt. So zeigt die Oberfläche nie einen
 * Zustand, den das Backend nicht hat – wichtig, sobald dahinter echtes Geld liegt.
 */

import { LivePanel, liveLines } from "./livepanel";
import type { Connection, LiveState, PaperMessage, PaperState } from "./net";
import type { Markets } from "./markets";
import { bracketInfo, qtyForPercent, qtyFromInput, roundTick, tradeLines, type OrderKind, type OrderSide, type TradeLine } from "./paper";
import { coinOf, VENUE_LABEL, venueOf } from "./venue";

const KEY = "cockbook.trade.v1";
const LEVERAGES = [1, 2, 3, 5, 10, 15, 20, 25, 50, 75, 100, 125];

export interface MarketCtx {
  symbol: string;
  tick: number;
  step: number;
  bid: number;
  ask: number;
}

interface Saved {
  open: boolean;
  kind: OrderKind;
  unit: "coin" | "usdt";
  amount: string;
  clickTrading: boolean;
  /** Paper-Konto oder echtes Börsenkonto */
  mode: "paper" | "live";
}

const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

const usd = (v: number, sign = false) =>
  `${sign && v > 0 ? "+" : ""}${v.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} $`;
const cls = (v: number) => (v > 0 ? "up" : v < 0 ? "down" : "");

export class TradePanel {
  readonly root: HTMLElement;
  state: PaperState | null = null;
  private saved: Saved = { open: true, kind: "limit", unit: "usdt", amount: "1000", clickTrading: false, mode: "paper" };
  private ctx: MarketCtx | null = null;
  private dec = 2;
  private pending = new Map<number, string>();
  // Elemente
  private acct: HTMLElement;
  private title: HTMLElement;
  private lev: HTMLSelectElement;
  private kindBtns = new Map<OrderKind, HTMLButtonElement>();
  private priceRow: HTMLElement;
  private price: HTMLInputElement;
  private triggerRow: HTMLElement;
  private trigger: HTMLInputElement;
  private amount: HTMLInputElement;
  private unitBtn: HTMLButtonElement;
  private summary: HTMLElement;
  private tp: HTMLInputElement;
  private sl: HTMLInputElement;
  private bracket: HTMLElement;
  private reduce: HTMLInputElement;
  private buyBtn: HTMLButtonElement;
  private sellBtn: HTMLButtonElement;
  private click: HTMLInputElement;
  private disabledNote: HTMLElement;
  private posBox: HTMLElement;
  private ordersBox: HTMLElement;
  private otherBox: HTMLElement;
  private historyBox: HTMLElement;
  private toasts: HTMLElement;
  private live: LivePanel;
  private badge: HTMLElement;
  private modeBtns = new Map<"paper" | "live", HTMLButtonElement>();
  /** nur im Paper-Modus sichtbar */
  private paperEls: HTMLElement[] = [];

  constructor(
    root: HTMLElement,
    private conn: Connection,
    private markets: Markets,
    private onOpenChange: () => void,
    private onSelectSymbol: (symbol: string) => void,
  ) {
    this.root = root;
    try {
      const s = JSON.parse(localStorage.getItem(KEY) ?? "null") as Partial<Saved> | null;
      if (s) this.saved = { ...this.saved, ...s };
    } catch {
      /* egal */
    }
    root.classList.add("trade");

    const head = el("div", "trade-head");
    this.badge = el("span", "badge paper", "PAPER");
    head.append(this.badge, el("span", "trade-h", "Handel"));
    const modes = el("div", "seg trade-modes");
    for (const [m, label] of [["paper", "Paper"], ["live", "Bitunix (echt)"]] as const) {
      const b = el("button", "", label);
      b.type = "button";
      b.addEventListener("click", () => this.setMode(m));
      this.modeBtns.set(m, b);
      modes.append(b);
    }
    this.live = new LivePanel(markets, onSelectSymbol);
    const reset = el("button", "trade-link", "Konto zurücksetzen");
    reset.type = "button";
    reset.addEventListener("click", () => {
      if (confirm("Paper-Konto zurücksetzen? Positionen, Orders und Verlauf werden gelöscht.")) this.send("reset", { confirm: true });
    });
    head.append(reset);
    this.acct = el("div", "trade-acct");

    // --- Ordermaske ---
    const mask = el("section", "trade-mask");
    const top = el("div", "trade-row");
    this.title = el("span", "trade-sym");
    this.lev = el("select", "trade-lev");
    this.lev.title = "Hebel (nur ohne offene Position änderbar)";
    this.lev.addEventListener("change", () => {
      if (this.ctx) this.send("leverage", { symbol: this.ctx.symbol, value: Number(this.lev.value) });
    });
    top.append(this.title, this.lev);

    const kinds = el("div", "seg trade-kinds");
    for (const [k, label] of [["market", "Market"], ["limit", "Limit"], ["stop", "Stop"]] as const) {
      const b = el("button", "", label);
      b.type = "button";
      b.addEventListener("click", () => this.setKind(k));
      this.kindBtns.set(k, b);
      kinds.append(b);
    }

    this.price = this.numInput("Limitpreis");
    this.priceRow = this.field("Preis", this.price, ...this.quoteButtons(this.price));
    this.trigger = this.numInput("Auslösepreis");
    this.triggerRow = this.field("Auslöser", this.trigger);
    this.triggerRow.title = "Stop-Market: wird zur Market-Order, sobald der Mid diesen Preis erreicht";

    this.amount = this.numInput("Menge");
    this.amount.value = this.saved.amount;
    this.unitBtn = el("button", "trade-unit");
    this.unitBtn.type = "button";
    this.unitBtn.title = "Menge in Coin oder in USDT (Nominalwert) eingeben";
    this.unitBtn.addEventListener("click", () => {
      this.saved.unit = this.saved.unit === "coin" ? "usdt" : "coin";
      this.save();
      this.refresh();
    });
    const amountRow = this.field("Menge", this.amount, this.unitBtn);
    const pct = el("div", "trade-pct");
    for (const p of [10, 25, 50, 100]) {
      const b = el("button", "", `${p} %`);
      b.type = "button";
      b.title = `${p} % der freien Margin × Hebel`;
      b.addEventListener("click", () => this.setPercent(p));
      pct.append(b);
    }
    this.summary = el("div", "trade-summary");

    this.tp = this.numInput("Take-Profit (Preis)");
    this.sl = this.numInput("Stop-Loss (Preis)");
    this.bracket = el("div", "trade-bracket");
    const reduceLab = el("label", "toggle");
    this.reduce = el("input");
    this.reduce.type = "checkbox";
    reduceLab.append(this.reduce, el("span", "", "Reduce-Only (nur verkleinern/schließen)"));

    const btns = el("div", "trade-btns");
    this.buyBtn = el("button", "trade-buy");
    this.sellBtn = el("button", "trade-sell");
    for (const [b, side] of [[this.buyBtn, "buy"], [this.sellBtn, "sell"]] as const) {
      b.type = "button";
      b.addEventListener("click", () => this.submit(side));
    }
    btns.append(this.buyBtn, this.sellBtn);

    const clickLab = el("label", "toggle trade-click");
    this.click = el("input");
    this.click.type = "checkbox";
    this.click.checked = this.saved.clickTrading;
    this.click.addEventListener("change", () => {
      this.saved.clickTrading = this.click.checked;
      this.save();
    });
    clickLab.append(this.click, el("span", "", "Klick-Trading"));
    clickLab.title =
      "DOM: Klick in BID = Kauf, in ASK = Verkauf (auf der anderen Seite des Kurses als Stop). " +
      "Chart: Strg+Klick unter dem Kurs = Kauf-Limit, darüber = Verkaufs-Limit. Menge aus der Maske.";
    this.disabledNote = el("div", "trade-note");

    this.price.addEventListener("input", () => this.refresh());
    this.trigger.addEventListener("input", () => this.refresh());
    this.amount.addEventListener("input", () => {
      this.saved.amount = this.amount.value;
      this.save();
      this.refresh();
    });
    this.tp.addEventListener("input", () => this.refresh());
    this.sl.addEventListener("input", () => this.refresh());

    mask.append(
      top,
      kinds,
      this.priceRow,
      this.triggerRow,
      amountRow,
      pct,
      this.summary,
      this.field("TP", this.tp),
      this.field("SL", this.sl),
      this.bracket,
      reduceLab,
      btns,
      clickLab,
      this.disabledNote,
    );

    this.posBox = el("section", "trade-pos");
    this.ordersBox = el("section", "trade-orders");
    this.otherBox = el("section", "trade-other");
    this.historyBox = el("section", "trade-history");
    const kill = el("button", "trade-kill", "Alles schließen (Kill-Switch)");
    kill.type = "button";
    kill.title = "Alle Positionen schließen und alle Orders löschen – aller Coins";
    kill.addEventListener("click", () => {
      if (confirm("Alle Positionen schließen und alle Orders löschen?")) this.send("close_all");
    });
    this.toasts = el("div", "trade-toasts");
    root.append(head, modes, this.acct, mask, this.posBox, this.ordersBox, this.otherBox, this.historyBox, kill, this.live.root, this.toasts);
    this.paperEls = [reset, this.acct, mask, this.posBox, this.ordersBox, this.otherBox, this.historyBox, kill];
    this.setKind(this.saved.kind);
    this.setMode(this.saved.mode, false); // false: main hält das Panel hier noch nicht
    root.hidden = !this.saved.open;
  }

  // --- Öffentlich ----------------------------------------------------------------------

  get open(): boolean {
    return !this.root.hidden;
  }

  toggle(): void {
    this.root.hidden = !this.root.hidden;
    this.saved.open = !this.root.hidden;
    this.save();
    this.onOpenChange();
  }

  get clickTrading(): boolean {
    return this.saved.mode === "paper" && this.saved.clickTrading && this.tradable();
  }

  get mode(): "paper" | "live" {
    return this.saved.mode;
  }

  /** Linien für den Chart – aus dem gerade gewählten Konto. */
  lines(symbol: string): TradeLine[] {
    if (this.saved.mode === "live") return liveLines(symbol, this.live.state);
    return this.state ? tradeLines(symbol, this.state.positions, this.state.orders) : [];
  }

  onLive(m: LiveState): void {
    this.live.state = m;
    if (this.saved.mode === "live") this.live.render(this.ctx?.symbol ?? null, this.dec);
  }

  private setMode(m: "paper" | "live", notify = true): void {
    this.saved.mode = m;
    this.save();
    for (const [k, b] of this.modeBtns) b.classList.toggle("on", k === m);
    for (const e of this.paperEls) e.hidden = m !== "paper";
    this.live.root.hidden = m !== "live";
    this.badge.textContent = m === "paper" ? "PAPER" : "LIVE";
    this.badge.className = `badge ${m === "paper" ? "paper" : "live-real"}`;
    if (m === "live") this.live.render(this.ctx?.symbol ?? null, this.dec);
    if (notify) this.onOpenChange(); // Chart-Linien des anderen Kontos zeichnen
  }

  /** Aktueller Markt (Symbol, Raster, BBO) – bei jedem Tick aus main. */
  setMarket(ctx: MarketCtx | null): void {
    const symbolChanged = ctx?.symbol !== this.ctx?.symbol;
    this.ctx = ctx;
    if (ctx) this.dec = Math.max(0, Math.round(-Math.log10(ctx.tick)));
    if (symbolChanged) {
      this.price.value = "";
      this.trigger.value = "";
      this.tp.value = "";
      this.sl.value = "";
      this.renderState();
      if (this.saved.mode === "live") this.live.render(ctx?.symbol ?? null, this.dec);
    }
    this.refresh();
  }

  onMessage(m: PaperMessage): void {
    if (m.type === "paper_state") {
      this.state = m;
      this.renderState();
      this.refresh();
    } else if (m.type === "paper_result") {
      const what = m.req !== null ? this.pending.get(m.req) : undefined;
      if (m.req !== null) this.pending.delete(m.req);
      if (m.message) this.toast(m.ok ? "ok" : "error", m.message);
      else if (!m.ok && what) this.toast("error", `${what} fehlgeschlagen`);
    } else {
      this.toast(m.level, m.message);
    }
  }

  /** Order aus Klick im DOM/Chart (Menge aus der Maske). */
  placeAt(side: OrderSide, kind: OrderKind, price: number): void {
    const ctx = this.ctx;
    if (!ctx || !this.tradable()) return;
    const qty = this.qty(price);
    if (qty <= 0) {
      this.toast("error", "Menge in der Ordermaske eintragen");
      return;
    }
    const p = roundTick(price, ctx.tick);
    this.send("order", {
      symbol: ctx.symbol,
      side,
      kind,
      qty: String(qty),
      ...(kind === "stop" ? { trigger: String(p) } : { price: String(p) }),
    });
  }

  /** Linie im Chart gezogen: Order verschieben bzw. TP/SL ändern. */
  dragTo(target: { type: "order"; id: string; field: "price" | "trigger" } | { type: "tp" | "sl"; symbol: string }, price: number): void {
    const ctx = this.ctx;
    if (!ctx) return;
    const p = String(roundTick(price, ctx.tick));
    if (target.type === "order") this.send("modify", { id: target.id, [target.field]: p });
    else this.setTpSl(target.symbol, target.type, p);
  }

  /** × an einer Linie */
  cancelLine(target: { type: "order"; id: string } | { type: "tp" | "sl"; symbol: string }): void {
    if (target.type === "order") this.send("cancel", { id: target.id });
    else this.setTpSl(target.symbol, target.type, null);
  }

  // --- Intern --------------------------------------------------------------------------

  private send(op: string, payload: Record<string, unknown> = {}): void {
    const req = this.conn.paper(op, payload);
    this.pending.set(req, op);
  }

  private setTpSl(symbol: string, which: "tp" | "sl", value: string | null): void {
    const pos = this.state?.positions.find((p) => p.symbol === symbol);
    if (!pos) return;
    const tp = which === "tp" ? value : pos.tp !== null ? String(pos.tp) : null;
    const sl = which === "sl" ? value : pos.sl !== null ? String(pos.sl) : null;
    this.send("tpsl", { symbol, tp, sl });
  }

  private tradable(): boolean {
    return !!this.ctx && venueOf(this.ctx.symbol) !== "multi" && !!this.state;
  }

  private save(): void {
    try {
      localStorage.setItem(KEY, JSON.stringify(this.saved));
    } catch {
      /* egal */
    }
  }

  private numInput(placeholder: string): HTMLInputElement {
    const i = el("input", "trade-input");
    i.type = "text";
    i.inputMode = "decimal";
    i.placeholder = placeholder;
    i.autocomplete = "off";
    return i;
  }

  private field(label: string, ...nodes: HTMLElement[]): HTMLElement {
    const row = el("div", "trade-field");
    row.append(el("span", "trade-label", label), ...nodes);
    return row;
  }

  private quoteButtons(target: HTMLInputElement): HTMLElement[] {
    return (["Bid", "Mid", "Ask"] as const).map((q) => {
      const b = el("button", "trade-q", q);
      b.type = "button";
      b.title = `${q === "Mid" ? "Mitte" : q} übernehmen`;
      b.addEventListener("click", () => {
        const c = this.ctx;
        if (!c) return;
        const v = q === "Bid" ? c.bid : q === "Ask" ? c.ask : roundTick((c.bid + c.ask) / 2, c.tick);
        target.value = v.toFixed(this.dec);
        this.refresh();
      });
      return b;
    });
  }

  private setKind(k: OrderKind): void {
    this.saved.kind = k;
    this.save();
    for (const [kk, b] of this.kindBtns) b.classList.toggle("on", kk === k);
    this.priceRow.hidden = k !== "limit";
    this.triggerRow.hidden = k !== "stop";
    this.refresh();
  }

  private num(i: HTMLInputElement): number | null {
    const v = Number(i.value.replace(",", ".").trim());
    return i.value.trim() && Number.isFinite(v) && v > 0 ? v : null;
  }

  /** Referenzpreis der Order (für Menge in USDT, Margin, TP/SL-Rechnung). */
  private refPrice(side: OrderSide): number | null {
    const c = this.ctx;
    if (!c) return null;
    if (this.saved.kind === "limit") return this.num(this.price);
    if (this.saved.kind === "stop") return this.num(this.trigger);
    return side === "buy" ? c.ask : c.bid;
  }

  private qty(price: number | null): number {
    const c = this.ctx;
    const amount = this.num(this.amount);
    if (!c || amount === null || price === null) return 0;
    return qtyFromInput(amount, this.saved.unit, price, c.step);
  }

  private leverage(): number {
    const s = this.state;
    const sym = this.ctx?.symbol;
    if (!s || !sym) return 10;
    const pos = s.positions.find((p) => p.symbol === sym);
    return pos?.leverage ?? s.leverage[sym] ?? s.account.default_leverage;
  }

  private setPercent(p: number): void {
    const c = this.ctx;
    const s = this.state;
    if (!c || !s) return;
    const price = this.refPrice("buy") ?? (c.bid + c.ask) / 2;
    const fee = venueOf(c.symbol) === "bitunix" ? 0.0006 : 0.0005;
    const q = qtyForPercent(p, s.account.available, this.leverage(), price, c.step, fee);
    this.amount.value = this.saved.unit === "coin" ? String(q) : (q * price).toFixed(2);
    this.saved.amount = this.amount.value;
    this.save();
    this.refresh();
  }

  private submit(side: OrderSide): void {
    const c = this.ctx;
    if (!c || !this.tradable()) return;
    const kind = this.saved.kind;
    const ref = this.refPrice(side);
    if (kind !== "market" && ref === null) {
      this.toast("error", kind === "limit" ? "Limitpreis eintragen" : "Auslösepreis eintragen");
      return;
    }
    const qty = this.qty(ref);
    if (qty <= 0) {
      this.toast("error", `Menge zu klein (min. ${c.step} ${coinOf(c.symbol).replace(/USDT$/, "")})`);
      return;
    }
    const payload: Record<string, unknown> = { symbol: c.symbol, side, kind, qty: String(qty), reduce_only: this.reduce.checked };
    if (kind === "limit") payload.price = String(ref);
    if (kind === "stop") payload.trigger = String(ref);
    const tp = this.num(this.tp);
    const sl = this.num(this.sl);
    if (tp !== null) payload.tp = String(tp);
    if (sl !== null) payload.sl = String(sl);
    this.send("order", payload);
  }

  /** Abhängige Anzeigen (Zusammenfassung, Knöpfe, TP/SL-Info) neu berechnen. */
  refresh(): void {
    const c = this.ctx;
    const s = this.state;
    const coin = c ? (this.markets.bySymbol.get(c.symbol)?.label ?? coinOf(c.symbol)) : "–";
    this.unitBtn.textContent = this.saved.unit === "coin" ? coin : "USDT";
    const venue = c ? venueOf(c.symbol) : null;
    this.title.textContent = c ? `${coin} · ${VENUE_LABEL[venue!]}` : "kein Coin";
    const multi = venue === "multi";
    const ok = !!c && !multi && !!s;
    this.disabledNote.hidden = ok;
    this.disabledNote.textContent = !s
      ? "Warte auf das Backend …"
      : multi
        ? "Im kombinierten Buch kann man nicht handeln – oben Binance oder Bitunix wählen."
        : "";
    for (const b of [this.buyBtn, this.sellBtn]) b.disabled = !ok;
    // Hebel-Auswahl
    const max = s?.account.max_leverage ?? 50;
    const lev = this.leverage();
    const opts = LEVERAGES.filter((l) => l <= max);
    if (!opts.includes(lev)) opts.push(lev);
    if (this.lev.options.length !== opts.length) {
      this.lev.replaceChildren(...opts.sort((a, b) => a - b).map((l) => el("option", "", `${l}x`)));
      opts.forEach((l, i) => (this.lev.options[i]!.value = String(l)));
    }
    if (document.activeElement !== this.lev) this.lev.value = String(lev);
    this.lev.disabled = !ok || !!s?.positions.some((p) => p.symbol === c?.symbol);
    // Zusammenfassung
    const kindTxt = { market: "Market", limit: "Limit", stop: "Stop" }[this.saved.kind];
    this.buyBtn.textContent = `${kindTxt} Kauf / Long`;
    this.sellBtn.textContent = `${kindTxt} Verkauf / Short`;
    const ref = this.refPrice("buy");
    const qty = this.qty(ref);
    if (c && ref !== null && qty > 0) {
      const notional = qty * ref;
      const fee = notional * (this.saved.kind === "limit" ? 0.0002 : venue === "bitunix" ? 0.0006 : 0.0005);
      this.summary.textContent =
        `${qty.toLocaleString("de-DE", { maximumFractionDigits: 6 })} ${coin} · Wert ${usd(notional)} · ` +
        `Margin ${usd(notional / lev)} · Gebühr ≈ ${usd(fee)}`;
    } else this.summary.textContent = c ? `min. ${c.step} ${coin}` : "";
    // TP/SL-Info (für einen Kauf gerechnet; bei Verkauf umgekehrt)
    const tp = this.num(this.tp);
    const sl = this.num(this.sl);
    if (ref !== null && qty > 0 && (tp !== null || sl !== null)) {
      const side: OrderSide = tp !== null ? (tp > ref ? "buy" : "sell") : sl! < ref ? "buy" : "sell";
      const b = bracketInfo(side, ref, qty, tp, sl);
      const parts: string[] = [side === "buy" ? "für Long:" : "für Short:"];
      if (b.tpPnl !== null) parts.push(`TP ${usd(b.tpPnl, true)} (${b.tpPct!.toFixed(2)} %)`);
      if (b.slPnl !== null) parts.push(`SL ${usd(b.slPnl, true)} (${b.slPct!.toFixed(2)} %)`);
      if (b.rr !== null) parts.push(`CRV ${b.rr.toFixed(2)}`);
      this.bracket.textContent = parts.join(" · ");
    } else this.bracket.textContent = "TP/SL optional – leer = keins";
  }

  private renderState(): void {
    const s = this.state;
    if (!s) return;
    const a = s.account;
    this.acct.replaceChildren(
      this.kv("Equity", usd(a.equity)),
      this.kv("Verfügbar", usd(a.available)),
      this.kv("uPnL", usd(a.upnl, true), cls(a.upnl)),
      this.kv("Heute", usd(a.day_realized, true), cls(a.day_realized)),
    );
    if (a.loss_limit_hit) this.acct.append(el("div", "trade-warn", "Tagesverlust-Limit erreicht – nur noch Schließen"));
    const sym = this.ctx?.symbol;
    // Position des aktuellen Coins (nicht neu bauen, während dort getippt wird)
    const pos = s.positions.find((p) => p.symbol === sym);
    const typing = this.posBox.contains(document.activeElement);
    if (!typing) this.posBox.replaceChildren(el("h4", "", "Position"));
    if (typing) {
      /* Eingabe läuft – Karte beim nächsten Zustand ohne Fokus aktualisieren */
    } else if (!pos) this.posBox.append(el("div", "trade-empty", "keine"));
    else {
      const long = pos.qty > 0;
      const card = el("div", `trade-card ${long ? "long" : "short"}`);
      const f = (v: number) => v.toLocaleString("de-DE", { minimumFractionDigits: this.dec, maximumFractionDigits: this.dec });
      card.append(
        el("div", "trade-card-h", `${long ? "Long" : "Short"} ${Math.abs(pos.qty).toLocaleString("de-DE", { maximumFractionDigits: 6 })} · ${pos.leverage}x`),
        this.kv("Einstieg", f(pos.entry)),
        this.kv("Break-even", f(pos.breakeven)),
        this.kv("Mark", f(pos.mark)),
        this.kv("uPnL", `${usd(pos.upnl, true)} (${pos.roe.toFixed(1)} %)`, cls(pos.upnl)),
        this.kv("Liquidation", f(pos.liq)),
      );
      const tp = this.numInput("TP");
      const sl = this.numInput("SL");
      if (pos.tp !== null) tp.value = pos.tp.toFixed(this.dec);
      if (pos.sl !== null) sl.value = pos.sl.toFixed(this.dec);
      const set = el("button", "trade-small", "TP/SL setzen");
      set.type = "button";
      set.addEventListener("click", () =>
        this.send("tpsl", { symbol: pos.symbol, tp: tp.value.trim() || null, sl: sl.value.trim() || null }),
      );
      const close = el("button", "trade-small danger", "Schließen (Market)");
      close.type = "button";
      close.addEventListener("click", () => this.send("close", { symbol: pos.symbol }));
      const row = el("div", "trade-tpsl");
      row.append(tp, sl, set);
      card.append(row, close);
      this.posBox.append(card);
    }
    // Orders des aktuellen Coins
    const orders = s.orders.filter((o) => o.symbol === sym);
    const oh = el("h4", "", `Offene Orders (${orders.length})`);
    if (orders.length) {
      const all = el("button", "trade-link", "alle löschen");
      all.type = "button";
      all.addEventListener("click", () => this.send("cancel_all", { symbol: sym }));
      oh.append(all);
    }
    this.ordersBox.replaceChildren(oh);
    if (!orders.length) this.ordersBox.append(el("div", "trade-empty", "keine"));
    for (const o of orders) {
      const r = el("div", `trade-order ${o.side}`);
      const price = o.kind === "stop" ? o.trigger : o.price;
      const q = (o.qty - o.filled).toLocaleString("de-DE", { maximumFractionDigits: 6 });
      const px = price?.toLocaleString("de-DE", { minimumFractionDigits: this.dec, maximumFractionDigits: this.dec }) ?? "–";
      const txt = `${o.kind === "stop" ? "Stop" : "Limit"} ${o.side === "buy" ? "Kauf" : "Verkauf"} ${q} @ ${px}`;
      r.append(el("span", "", txt + (o.reduce_only ? " · RO" : "")));
      if (o.kind === "limit") {
        const ahead = o.queue > 0 ? `${o.queue.toLocaleString("de-DE", { maximumFractionDigits: 3 })} vor dir` : "vorne";
        const qq = el("span", "trade-queue", ahead);
        qq.title = "Menge, die laut Buch vor deiner Order in der Warteschlange liegt";
        r.append(qq);
      }
      const x = el("button", "trade-x", "×");
      x.type = "button";
      x.title = "Order löschen";
      x.addEventListener("click", () => this.send("cancel", { id: o.id }));
      r.append(x);
      this.ordersBox.append(r);
    }
    // Andere Coins (Positionen/Orders anderswo)
    const others = new Map<string, { upnl: number; orders: number; pos: boolean }>();
    for (const p of s.positions) if (p.symbol !== sym) others.set(p.symbol, { upnl: p.upnl, orders: 0, pos: true });
    for (const o of s.orders) {
      if (o.symbol === sym) continue;
      const e = others.get(o.symbol) ?? { upnl: 0, orders: 0, pos: false };
      e.orders++;
      others.set(o.symbol, e);
    }
    this.otherBox.replaceChildren();
    this.otherBox.hidden = others.size === 0;
    if (others.size) {
      this.otherBox.append(el("h4", "", "Andere Coins"));
      for (const [k, v] of others) {
        const b = el("button", "trade-other-row");
        b.type = "button";
        const label = `${this.markets.bySymbol.get(k)?.label ?? coinOf(k)} · ${VENUE_LABEL[venueOf(k)]}`;
        b.append(el("span", "", label), el("span", cls(v.upnl), v.pos ? usd(v.upnl, true) : `${v.orders} Order(s)`));
        b.addEventListener("click", () => this.onSelectSymbol(k));
        this.otherBox.append(b);
      }
    }
    // Verlauf
    const h = el("h4", "", "Verlauf");
    const csv = el("button", "trade-link", "CSV");
    csv.type = "button";
    csv.title = "Alle abgeschlossenen Trades als CSV herunterladen";
    csv.addEventListener("click", () => this.exportCsv());
    h.append(csv);
    this.historyBox.replaceChildren(h);
    const trades = [...s.trades].reverse().slice(0, 15);
    if (!trades.length) this.historyBox.append(el("div", "trade-empty", "noch keine abgeschlossenen Trades"));
    for (const t of trades) {
      const r = el("div", "trade-hist");
      const when = new Date(t.closed).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
      r.append(
        el("span", "", `${when} ${this.markets.bySymbol.get(t.symbol)?.label ?? coinOf(t.symbol)} ${t.side === "long" ? "Long" : "Short"}`),
        el("span", cls(t.pnl), usd(t.pnl, true)),
      );
      r.title = `${t.reason} · Einstieg ${t.entry} → Ausstieg ${t.exit.toFixed(this.dec)} · Gebühren ${usd(t.fees)}`;
      this.historyBox.append(r);
    }
  }

  private kv(k: string, v: string, c = ""): HTMLElement {
    const d = el("div", "kv");
    d.append(el("span", "k", k), el("span", `v ${c}`, v));
    return d;
  }

  private exportCsv(): void {
    const s = this.state;
    if (!s) return;
    const iso = (ms: number) => new Date(ms).toISOString();
    const rows = [["symbol", "side", "qty", "entry", "exit", "pnl_usdt", "fees_usdt", "opened_utc", "closed_utc", "reason"]];
    for (const t of s.trades) {
      rows.push([t.symbol, t.side, String(t.qty), String(t.entry), String(t.exit), t.pnl.toFixed(4), t.fees.toFixed(4), iso(t.opened), iso(t.closed), t.reason]);
    }
    const blob = new Blob([rows.map((r) => r.join(",")).join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `cockbook-paper-trades-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  private toast(level: string, text: string): void {
    const t = el("div", `trade-toast ${level}`, text);
    this.toasts.append(t);
    setTimeout(() => t.remove(), level === "error" ? 6000 : 3500);
    while (this.toasts.childElementCount > 4) this.toasts.firstElementChild?.remove();
  }
}
