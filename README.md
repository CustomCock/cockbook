# CockBook

> **Status: Work in Progress** – Liquidity-Heatmap (live + Langzeit) mit Trades, Kerzen und RSI (WebGL)
> für die Top-20-Coins sowie Gold und Silber (Binance USDⓈ-M), Seitenleiste mit Derivate-Kennzahlen.
> DOM, CVD, Alerts, Replay usw. folgen.

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

Coins werden oben links im **Dropdown** gewählt (Klick oder Taste `/`, Tippen filtert, ↑/↓ + Enter).
Die Liste zeigt je Coin Preis, 24h, Veränderung seit 1. Januar, RSI auf Tageskerzen und den
Orderbuch-Status (Punkt); Gold und Silber stehen unter „Rohstoffe (TradFi)“ mit ihrer Handelszeit.
Die Coins stehen in `config.yaml` unter `symbols` (Standard: Top 20 nach Marktkapitalisierung ohne
Stablecoins, dazu XAUUSDT und XAGUSDT). Symbole, die es auf Binance USDⓈ-M nicht gibt, werden mit
einer Warnung übersprungen.

Kennzahlen (Preis, 24h, Funding …) kommen für alle Coins über Gesamtmarkt-Streams. Das **Orderbuch**
(Heatmap) läuft ab Start nur für Coins mit `always_on: true` (Standard: BTC, ETH, XRP, SOL); jeder andere
Coin startet beim ersten Auswählen und läuft dann weiter – die Heatmap beginnt also erst ab diesem
Moment. Gold und Silber sind TradFi-Kontrakte mit Handelspausen (z. B. am Wochenende); in einer Pause
kommen keine Orderbuch-Updates.

Die Seitenleiste enthält alle Funktionen zum Ein-/Ausschalten (gelten für alle Coins):

- **Chart:** Liquidity-Heatmap, Langzeit-Heatmap, Weich zeichnen, Trade-Bubbles, Best Bid/Ask,
  Liquidationen, Preisgitter, Kerzen (1m/3m/5m/10m), RSI-Fenster unter dem Chart
- **Langzeit-Heatmap:** ältere Liquidität (bis 2 h) als gröbere Ebene unter der Live-Heatmap
  (Mittel über 2 s und je 2 Preis-Stufen) – sichtbar beim Herauszoomen in der Zeit (Shift+Mausrad)
  oder Zurückziehen. Die Live-Heatmap hat volle Auflösung für die letzten 5 min.
- **ATH & Zyklen:** Allzeithoch, Abstand, Tage seit ATH, 52-Wochen-Spanne; Marktzyklus (Halving-Zyklen
  oder eigener Zeitraum) mit Länge, Fortschritt, Zyklus-Hoch/-Tief und Performance
- **Indikatoren:** RSI(14) nach Wilder für 1m, 5m, 15m, 1h und 1D; im RSI-Fenster im Intervall der Kerzen
- **Derivate:** Mark-/Indexpreis, Funding Rate (mit Countdown), Open Interest (Δ 5 min / 1 h),
  Longs vs Shorts, Taker Buy/Sell, 24h-Statistik, Liquidationen des Gesamtmarkts (sekündlich)
- **Geplant:** Liquidation Heatmap, Funding-Heatmap, RSI-Heatmap aller Coins, Depth Delta,
  Multi-Börsen-Orderbuch …

Bedienung: Mausrad = Preis-Zoom, Shift+Mausrad (oder Mausrad über der Zeitachse) = Zeit-Zoom,
Ziehen = verschieben (auch zurück in der Zeit), Doppelklick oder `C` = zurück zu Live mit
Auto-Zentrierung, `F` = Preisbereich an die sichtbaren Kerzen anpassen, `/` = Coin suchen. Regler „Kontrast“ und „Schwelle“ steuern die Farbskala; Einstellungen werden
im Browser gespeichert.

Ohne GPU (z. B. in einer VM) rendert der Browser per Software. CockBook erkennt das und
zeichnet die Heatmap dann intern mit halber Auflösung (Anzeige oben rechts: „Software-GL“).
Override per URL: `?scale=1`.

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
