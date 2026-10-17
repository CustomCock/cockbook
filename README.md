# CockBook

> **Status: Work in Progress** – Liquidity-Heatmap (live + Langzeit) mit Trades, Kerzen, RSI und
> DOM-Ladder (WebGL) für die Top-20-Coins sowie alle TradFi-Kontrakte von Binance USDⓈ-M (Aktien, ETFs,
> Rohstoffe), dazu alle Bitunix-Futures und ein kombiniertes Buch (Multibook Binance + Bitunix),
> Coin-Tabs, Seitenleiste mit Derivate-Kennzahlen. CVD, Alerts, Replay usw. folgen.

Freies, lokal laufendes Orderflow- & Liquidity-Heatmap-Tool für Krypto-Märkte, inspiriert von Bookmap.
Es nutzt ausschließlich öffentliche, kostenlose Börsen-Datenfeeds und läuft unter Ubuntu und Windows.

Geplanter Funktionsumfang: Liquidity-Heatmap, Trade-Bubbles, DOM, Time & Sales, CVD, Alerts,
Record & Replay, Multibook über mehrere Börsen, Orderflow-Indikatoren (Absorption, Sweeps, Icebergs,
Imbalance …), Paper-Trading und optional Live-Trading. Die Roadmap steht in [`PHASEN.md`](PHASEN.md),
die Architektur-Regeln in [`CLAUDE.md`](CLAUDE.md).

## Architektur (Kurzfassung)

```
Börse ──WS/REST──▶ Adapter ──normalisierte Events──▶ EventBus ──▶ Orderbuch / Indikatoren / Recorder
                                                         │
                                                         └──▶ WebSocket-Bridge (127.0.0.1) ──▶ Frontend (TS + WebGL)
```

- **Backend:** Python 3.12+, `asyncio`, `uv`, `pydantic`. Preise als int-Ticks, Mengen als `Decimal`, Zeit in ns.
- **Frontend:** TypeScript + Vite, Rendering per Canvas2D/WebGL.
- **Live und Replay** implementieren dasselbe Interface – alles dahinter merkt keinen Unterschied.

## Schnellstart (Entwicklung)

### Voraussetzungen

- [uv](https://docs.astral.sh/uv/) (Python-Umgebung)
- Node.js **≥ 22.12** mit npm. Das `nodejs`-Paket aus den Ubuntu-Paketquellen ist dafür meist zu alt;
  empfohlen ist [nvm](https://github.com/nvm-sh/nvm): `nvm install --lts` (Windows: Installer von
  [nodejs.org](https://nodejs.org), LTS-Version)
- `git` und `make` (Windows: z. B. über Git Bash)

Das Repo per `git clone` holen (nicht als ZIP), dann reicht später `git pull` für Updates:

```bash
git clone https://github.com/CustomCock/cockbook.git
cd cockbook
```

```bash
make install   # Backend (uv sync) + Frontend (npm install)
make hooks     # Pre-Commit-Hooks inkl. Secret-Scan aktivieren
make test      # pytest
make lint      # ruff, mypy, tsc
make dev       # Backend + Frontend-Dev-Server (http://127.0.0.1:5173)
```

### Heatmap

```bash
make dev       # Binance-Livedaten -> Backend (ws://127.0.0.1:8765) + Frontend http://127.0.0.1:5173
```

Coins werden oben links im **Coin-Menü** gewählt (Klick, Taste `/` oder `+` in der Tab-Leiste). Oben im
Menü wählt eine Leiste die Kategorie – **Krypto, Aktien, ETFs, Rohstoffe, Devisen** –, darunter stehen
die Coins mit Preis, 24h, Veränderung seit 1. Januar, RSI auf Tageskerzen, Orderbuch-Status (Punkt)
und bei TradFi der Handelsphase (offen, Vorbörse, Nachthandel, geschlossen). Klick auf einen
Spaltenkopf sortiert. Die Suche ist optional: einfach lostippen, sie durchsucht alle Kategorien.
Tastatur: ↑/↓ + Enter, ←/→ Kategorie, Esc.

Jeder gewählte Coin öffnet einen **Tab** (wie im Browser): Klick wechselt, `×` oder Mittelklick
schließt, Ziehen sortiert um; `Alt+1…9`, `Alt+←/→`, `Alt+W`. Die zuletzt benutzten Coins bleiben samt
Heatmap im Speicher, der Wechsel zurück ist sofort da. Die Tabs werden im Browser gespeichert.

Krypto-Coins stehen in `config.yaml` unter `symbols` (Standard: Top 20 nach Marktkapitalisierung ohne
Stablecoins, dazu Gold und Silber). **Aktien-, ETF-, Rohstoff- und Devisen-Kontrakte** (Binance
„TradFi-Perpetuals“, z. B. QQQ, SPY, AAPL, TSLA, NVDA, Öl) werden automatisch aus der Börse übernommen
(`tradfi_auto_discover`). Binance kennzeichnet sie nicht einheitlich und unterscheidet ETFs nicht von
Aktien – deshalb stehen bekannte Symbole mit ihrer Kategorie unter `tradfi_categories`; neue Listings
dort einfach ergänzen. Beim Start steht im Log, welche Kontrakt-Typen Binance liefert und welche
konfigurierten Symbole dort (noch) nicht handelbar sind. Symbole, die es auf
Binance USDⓈ-M nicht gibt, werden mit einer Warnung übersprungen.

Kennzahlen (Preis, 24h, Funding …) kommen für alle Coins über Gesamtmarkt-Streams. Das **Orderbuch**
(Heatmap) läuft ab Start nur für Coins mit `always_on: true` (Standard: BTC, ETH, XRP, SOL); jeder andere
Coin startet beim ersten Auswählen und läuft dann weiter – die Heatmap beginnt also erst ab diesem
Moment. Gold und Silber sind TradFi-Kontrakte mit Handelspausen (z. B. am Wochenende); in einer Pause
kommen keine Orderbuch-Updates.

**Multibook (Binance + Bitunix):** Im Coin-Menü wählt die obere Leiste die Börse – **Binance**,
**Bitunix** (alle handelbaren Bitunix-Futures) oder **Kombiniert** (Σ: Liquidität und Trades beider
Börsen addiert). Neben dem Coin-Knopf wechselt ein Schnell-Umschalter für denselben Coin die Börse.
Bitunix-Orderbücher starten erst bei Auswahl (Bitunix schickt bei jeder Änderung das ganze Buch).
Das kombinierte Buch rechnet in der feinsten gemeinsamen Tick-Größe und mit dem gröberen der beiden
Preis-Buckets; es gilt nur als synchron, wenn beide Bücher synchron sind. Funding, Open Interest usw.
gibt es nur von Binance (im kombinierten Buch von dort übernommen); die Langzeit-Heatmap des
kombinierten Buchs zeigt die von Binance.

Die Seitenleiste enthält alle Funktionen zum Ein-/Ausschalten (gelten für alle Coins):

- **Chart:** Liquidity-Heatmap, Weich zeichnen, Trade-Bubbles, Best Bid/Ask, Liquidationen, Preisgitter,
  Kerzen (1m/3m/5m/10m), Orderbuch (DOM), RSI-Fenster unter dem Chart
- **Heatmap:** zeigt alles, was das Orderbuch kennt (bis ±8000 Preis-Stufen um den Kurs); Liquidität
  bleibt an ihrem Preis stehen. Wichtig zum Verständnis: Binance liefert per REST nur die besten 1000
  Level je Seite – bei BTC nur etwa ±0,2–0,4 % um den Kurs. Weiter entfernte Level lernt das Buch erst,
  wenn sich dort etwas ändert; nach einem Neu-Sync bleiben sie als „zuletzt bekannt“ stehen
  (`memory_max_age_s`). Der bekannte Bereich wächst also mit der Zeit. Herauszoomen im Preis nutzt eine
  zweite, gröbere Preis-Ebene (je 8 Stufen zusammengefasst).
- **Langzeit-Heatmap** (eigener Tab, Knopf oben in der Seitenleiste): **12h · 1D · 1W · 1M** umschaltbar,
  jede Stufe mit eigener Auflösung (30 s / 1 min / 10 min / 30 min je Spalte), dazu Kerzen
  (5m/15m/1h/4h) und Liquidationen je Spalte. Die Seitenleiste zeigt dort ATH & Zyklen, Derivate und
  Liquidationen. Daten sammelt das Backend, solange das Orderbuch eines Coins läuft (`always_on` =
  immer), und speichert sie unter `data/longterm/` – sie überstehen Neustarts. Rechts daneben ein
  **Ø-Orderbuch** (mittleres Buch der Spalte am rechten Rand), unten **Volumen je Kerze**.
  Beim ersten Start: Kerzen und Volumen sind sofort für den ganzen Zeitraum da, die laufende
  Heatmap-Spalte erscheint sofort (nicht erst am Intervallende), und solange weniger gesammelt
  ist als der Zeitraum, zoomt die Ansicht auf die vorhandenen Daten (mindestens 1 h). Ältere
  Orderbuch-Historie gibt es nicht: Keine Börse stellt sie kostenlos bereit.
- **Orderbuch (DOM):** rechts neben der Preisachse, auf derselben Preisachse wie die Heatmap: Bid/Ask je
  Preiszeile (zurückgescrollt: zum betrachteten Zeitpunkt), gehandeltes Volumen (Kauf/Verkauf) und
  Delta im sichtbaren Zeitraum. Zeilen fassen je nach Zoom 1, 2, 5, 10 … Stufen zusammen. Klick auf die
  DOM-Kopfzeile schaltet Menge ↔ USDT um.
- **ATH & Zyklen** (im Langzeit-Tab): Allzeithoch, Abstand, Tage seit ATH, 52-Wochen-Spanne; Marktzyklus (Halving-Zyklen
  oder eigener Zeitraum) mit Länge, Fortschritt, Zyklus-Hoch/-Tief und Performance
- **Indikatoren:** RSI(14) nach Wilder für 1m, 5m, 15m, 1h und 1D; im RSI-Fenster im Intervall der Kerzen
- **Derivate:** Mark-/Indexpreis, Funding Rate (mit Countdown), Open Interest (Δ 5 min / 1 h),
  Longs vs Shorts, Taker Buy/Sell, 24h-Statistik, Liquidationen des Gesamtmarkts (sekündlich)
- **Geplant:** Liquidation Heatmap, Funding-Heatmap, RSI-Heatmap aller Coins, Depth Delta …

**Paper-Trading** (Panel rechts, Knopf „Handel“ oder Taste `T`): simuliertes Futures-Konto gegen die
echten Orderbücher von Binance bzw. Bitunix – kein Konto, keine API-Keys nötig.

- **Ordermaske:** Market, Limit, Stop (Stop-Market); Menge in Coin oder USDT, 10/25/50/100 % der freien
  Margin; Hebel; TP/SL direkt an der Order (mit Gewinn/Verlust und Chance/Risiko); Reduce-Only.
- **Klick-Trading** (Häkchen im Panel, standardmäßig aus): im DOM Klick in BID = Kauf, in ASK = Verkauf
  (jenseits des Kurses wird daraus ein Stop); im Chart `Strg`+Klick unter dem Kurs = Kauf-Limit, darüber
  = Verkaufs-Limit. Die Menge kommt aus der Maske.
- **Im Chart:** Linien für Einstieg (mit PnL), Break-even (inkl. Gebühren), Liquidation, TP/SL und offene
  Orders. Order- und TP/SL-Linien lassen sich ziehen, × löscht.
- **Fill-Modell:** Market läuft durchs echte Buch (Slippage). Limit stellt sich hinten in die
  Warteschlange seines Preises („x vor dir“) und wird erst gefüllt, wenn das Volumen davor gehandelt
  bzw. gecancelt ist oder der Preis durchläuft. Stop, TP/SL und Liquidation lösen am Mid aus.
  Isolierte Margin, One-Way-Modus, Gebühren je Börse in `config.yaml`; Funding wird nicht simuliert.
- **Grenzen** wie später beim Live-Trading: max. Hebel, max. Positionsgröße, optional Tagesverlust-Limit,
  „Alles schließen“ als Kill-Switch. Konto und Verlauf (CSV-Export) liegen in `data/paper/` und
  überstehen Neustarts.

Bedienung: Mausrad = Preis-Zoom, Shift+Mausrad (oder Mausrad über der Zeitachse) = Zeit-Zoom,
Ziehen = verschieben (auch zurück in der Zeit), Doppelklick oder `C` = zurück zu Live mit
Auto-Zentrierung, `F` = Preisbereich an die sichtbaren Kerzen anpassen, `/` = Coin suchen, `Alt+1…9` / `Alt+←→` = Tabs, `Alt+W` = Tab schließen. Regler „Kontrast“ und „Schwelle“ steuern die Farbskala; Einstellungen werden
im Browser gespeichert.

Ohne GPU (z. B. in einer VM) rendert der Browser per Software. CockBook erkennt das und
zeichnet die Heatmap dann intern mit halber Auflösung (Anzeige oben rechts: „Software-GL“).
Override per URL: `?scale=1`.

### Auf einem Server betreiben (kostenlos)

CockBook kann komplett auf einer Gratis-VM laufen (Oracle Cloud „Always Free“), erreichbar per
HTTPS von überall, geschützt mit einem Zugangs-Token, rund um die Uhr sammelnd:

```bash
git clone https://github.com/CustomCock/cockbook.git
cd cockbook/deploy && cp .env.example .env    # Adresse + Token eintragen
docker compose up -d --build
```

Schritt-für-Schritt-Anleitung (Konto, VM, Ports, kostenlose Adresse, Start):
[`deploy/README.md`](deploy/README.md).

### Orderbuch & Aufzeichnung

```bash
make book      # Top-10 Bid/Ask live im Terminal (python -m cockbook.debug_book)
make record    # nur aufzeichnen (Coins mit always_on) -> data/<exchange>/<symbol>/YYYY-MM-DDTHH.jsonl.zst
```

Hinweis: Binance sperrt den Zugriff aus bestimmten Ländern (HTTP 451, „restricted location“).
CockBook nutzt ausschließlich die offiziellen Endpoints und umgeht solche Sperren nicht.

Ohne `make`:

```bash
cd backend && uv sync && uv run pytest
uv run --project backend python -m cockbook --config config.yaml
cd frontend && npm install && npm run dev
```

Konfiguration: [`config.yaml`](config.yaml). **API-Keys niemals in Dateien eintragen** – nur über
Umgebungsvariablen oder den OS-Keyring.

## Hinweise

**Kein offizielles Produkt.** CockBook ist ein unabhängiges Hobby-/Open-Source-Projekt und steht in keiner
Verbindung zu Bookmap Ltd. und wird von ihr weder unterstützt noch gesponsert. „Bookmap" ist eine Marke der
Bookmap Ltd. und wird hier nur beschreibend verwendet.

**Risiko-Hinweis.** Diese Software ist keine Anlageberatung. Die Nutzung erfolgt vollständig auf eigenes
Risiko. Der Handel mit Kryptowährungen und Derivaten ist hochriskant; insbesondere Live-Trading über
Börsen-APIs kann zu erheblichen bis vollständigen Verlusten führen. Daten und Indikatoren können fehlerhaft,
verzögert oder unvollständig sein. Es gibt keinerlei Gewährleistung (siehe Lizenz).

## Lizenz

[MIT](LICENSE)
