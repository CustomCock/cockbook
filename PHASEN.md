# CockBook – Phasen-Prompts für Claude Code

Vorgehen: `CLAUDE.md` liegt im Projekt-Root. Dann pro Phase den Block in Claude Code einfügen. Erst weitermachen, wenn die Abnahmekriterien der Phase erfüllt sind.

---

## Phase 0 – Grundgerüst

```
Lies CLAUDE.md. Setze das Projekt-Grundgerüst auf:
- Repo-Struktur wie in CLAUDE.md, uv-Projekt fürs Backend, Vite+TypeScript fürs Frontend
- ruff, mypy, pytest konfiguriert; ein Makefile oder justfile mit: dev, test, lint, record, replay
- config.yaml mit Schema-Validierung (pydantic), Logging-Setup (strukturiert, Level per Config)
- core/events.py mit den normalisierten Event-Typen aus CLAUDE.md
- core/eventbus.py: asynchroner Pub/Sub, mehrere Subscriber pro Event-Typ, Backpressure-Strategie begründen
- .gitignore (data/, .env, node_modules, …)
- Das Repo wird öffentlich auf GitHub veröffentlicht: LICENSE (MIT), README-Grundgerüst mit Projektbeschreibung, Status „Work in Progress", Hinweis „nicht mit Bookmap Ltd. verbunden" und Risiko-Hinweis
- Pre-commit-Hook mit Secret-Scan (z. B. gitleaks oder detect-secrets), damit nie versehentlich ein API-Key im öffentlichen Repo landet
- git init, erster Commit
Zeig mir zuerst einen Plan, dann bauen.
Abnahme: `make test` und `make lint` laufen grün, Frontend-Dev-Server startet mit leerer Seite.
```

---

## Phase 1 – Binance-Adapter, Orderbuch, Recorder

```
Baue den ersten Adapter für Binance USDⓈ-M Futures, Symbol BTCUSDT (konfigurierbar).
- Hole die aktuelle Binance-Doku zur lokalen Orderbuch-Verwaltung (Diff-Depth-Stream + REST-Snapshot) und implementiere die Sync-Prozedur exakt danach. Referenz-Link in den Code.
- Trade-Stream (aggTrade oder trade – begründe die Wahl) mit Aggressor-Seite.
- Liquidations-Stream; dokumentiere im Code, ob Binance jedes Event oder gedrosselte Snapshots liefert.
- core/orderbook.py: Preise als int-Ticks, effiziente Struktur für Best Bid/Ask und Range-Abfragen. Begründe die Datenstruktur.
- Validierung: alle N Minuten einen frischen REST-Snapshot holen und mit dem lokalen Buch vergleichen (Top-Levels), Abweichung loggen.
- recorder/: rohe WebSocket-Nachrichten + Snapshots mit Empfangs-Timestamp als zstd-komprimiertes JSONL, stündliche Dateien unter data/<exchange>/<symbol>/.
- Reconnect mit Exponential Backoff, nach Reconnect vollständiger Neu-Sync.
- CLI: `make record` startet nur den Recorder; `python -m cockbook.debug_book` zeigt Top-10 Bid/Ask live im Terminal.
Tests: Orderbuch-Apply-Logik inkl. Lücken-Erkennung mit Fixtures.
Abnahme: Recorder läuft 30 Minuten ohne Desync-Warnung, Terminal-Ansicht stimmt mit der Binance-Webseite überein.
```

---

## Phase 2 – Heatmap-Rendering

```
Baue die Kernansicht: Liquidity-Heatmap mit Trades.
- Backend: aggregation/ erzeugt in festem Intervall (Config, Default 100 ms) eine Spalte: Liquidität pro Preis-Bucket (Bucket = Vielfaches der Tick-Größe, konfigurierbar) für einen Preisbereich um den Mid.
- server/: WebSocket-Bridge, sendet Heatmap-Spalten binär (MessagePack), Trades und BBO separat.
- Frontend: Heatmap als Ringpuffer-Textur (WebGL bevorzugt), neue Spalte rechts, scrollt nach links. Farbskala mit Kontrast-/Schwellwert-Regler (logarithmisch oder Perzentil-basiert – begründe).
- Overlay: Best-Bid/Ask-Linie, Trades als Bubbles (Größe ~ Volumen, Farbe nach Aggressor), Preisachse, Zeitachse.
- Interaktion: vertikaler Zoom/Pan, horizontaler Zeit-Zoom, Auto-Recentering mit konfigurierbarem Rand, Mouse-Hover zeigt Preis + Liquidität am Level.
- Ziel: flüssig (≥30 FPS) auf normalem Laptop. Miss die Performance und zeig mir die Zahlen.
Abnahme: Heatmap läuft live für BTCUSDT, große Walls sind sichtbar, Trades erscheinen am richtigen Preis.
```

---

## Phase 3 – Basis-Werkzeuge

```
Ergänze die Standard-Werkzeuge:
- Time & Sales Panel mit Größenfilter
- DOM-Ladder neben der Heatmap: Bid/Ask-Größe, gehandeltes Volumen pro Level, Delta pro Level
- CVD als Sub-Chart unter der Heatmap
- Large Trades: Schwellwert, hervorgehobene Bubbles + Alert. Trades mit identischem Timestamp/gleicher Taker-Order zusammenfassen (Large Lot Tracker).
- Alert-System: Regeln für Preis kreuzt Level, Volumen in Zeitfenster > X, Large Trade. Ausgabe als Desktop-Notification und optional Telegram-Bot (Token aus Env).
- Price Levels & Notizen: manuell setzen per Klick, aus CSV/JSON importieren, persistent speichern.
Jeder Indikator als eigenes Modul in indicators/ mit gemeinsamem Interface (on_event, state, render-Daten).
Abnahme: alles live sichtbar, Unit-Tests für CVD und Large-Lot-Aggregation.
```

---

## Phase 4 – Replay

```
Baue replay/ als Datenquelle mit demselben Interface wie der Live-Adapter.
- Liest die Aufzeichnungen aus Phase 1, rekonstruiert das Orderbuch durch dieselbe Sync-Logik.
- Steuerung aus dem Frontend: Play/Pause, Geschwindigkeit (0.25x–50x), Sprung zu Zeitpunkt. Für schnelles Springen periodische Orderbuch-Checkpoints beim Aufzeichnen oder beim ersten Laden anlegen – begründe den Ansatz.
- Im Frontend klar erkennbar, ob LIVE oder REPLAY läuft.
- Recorder als Daemon: systemd-Unit-Datei (Linux) und Anleitung für Windows (Aufgabenplanung), damit 24/7 aufgezeichnet werden kann. Rotation/Aufräumen alter Dateien per Config (Max-Tage, Max-GB).
Abnahme: Eine aufgezeichnete Stunde lässt sich abspielen und sieht identisch aus wie live; alle Indikatoren aus Phase 3 laufen im Replay mit.
```

---

## Phase 5 – Weitere Börsen & Multibook

```
Weitere Adapter, jeweils nach aktueller offizieller Doku:
- Bybit (linear perpetuals), OKX (SWAP, mit Checksummen-Prüfung), Coinbase Advanced Trade (Spot, Level-2 und – separat – Level-3/Full-Channel falls öffentlich verfügbar; prüfe den aktuellen Stand).
- Bitunix: prüfe, ob es einen öffentlichen WebSocket-Depth-Feed gibt. Falls ja, Adapter bauen; falls nicht, kurz berichten.
- Symbol-Normalisierung (BTCUSDT / BTC-USDT-SWAP / BTC-USD …), Tick-Größen, Quote-Währung. USD vs. USDT nicht stillschweigend gleichsetzen – konfigurierbar machen.
- Multibook: konsolidiertes Orderbuch über gewählte Börsen, Heatmap darauf umschaltbar.
- Cross BBO: beste Bid/Ask je Börse als Linien.
- Liquidations aller Futures-Börsen in einem Sub-Chart.
- Multi-Symbol: bis zu N Charts gleichzeitig, synchronisierte Zeitachse optional.
Abnahme: Multibook für BTC über mindestens 3 Börsen läuft stabil, jeder Adapter hat Sync-Tests.
```

---

## Phase 6 – Indikatoren

```
Implementiere als Module in indicators/, jeweils mit Config-Parametern und Unit-Tests:
- TA: RSI, MACD, ADX, OBV, Williams %R – selbst implementiert, keine TA-Library.
- Anchored VWAP (Anker per Klick), Point of Control / Volumenprofil der Session
- Footprint-Ansicht (Bid/Ask-Volumen pro Preis pro Kerze), Zeitrahmen umschaltbar
- Orderbuch-Imbalance (Top-N-Level) und Volume-Imbalance
- Strength Level: eigene, dokumentierte Metrik (z. B. Liquiditätsverhältnis über Zeit gewichtet) – Formel im Docstring erklären
- Outside Liquidity (Summe der Tiefe außerhalb des sichtbaren Bereichs) und Liquidity Marker (große Walls markieren, Lebensdauer tracken)
- Correlation Tracker (Rolling Correlation zwischen gewählten Symbolen)
- Market Pulse: Preisänderung über mehrere Zeitfenster als kompaktes Panel
Alle Indikatoren ein-/ausblendbar im Frontend, Einstellungen persistent.
Abnahme: alle Tests grün, alle Indikatoren live und im Replay nutzbar.
```

---

## Phase 7 – Orderflow-Heuristiken

```
Baue die Erkennungs-Indikatoren. Jede Heuristik: klare Definition im Docstring, alle Schwellwerte in config.yaml, Validierung gegen Replay-Daten.
- Sweeps: ein Taker-Event räumt mehrere Preislevel in sehr kurzer Zeit ab → Marker auf der Heatmap
- Absorption: hohes aggressives Volumen auf einem Level/engen Bereich, ohne dass der Preis durchbricht → Marker + Stärke
- Iceberg-Erkennung:
  (a) auf L2-Daten: Level wird gehandelt, Größe füllt sich wiederholt nach
  (b) auf Coinbase L3 (falls in Phase 5 verfügbar): Order-IDs nutzen für präzisere Erkennung
- Spoofing-Kandidaten: große Orders, die verschwinden, kurz bevor der Preis sie erreicht
Baue zusätzlich ein Auswertungsskript: läuft eine Heuristik über einen Replay-Zeitraum und gibt Treffer-Liste + Statistik aus, damit ich die Parameter tunen kann.
Abnahme: Marker erscheinen nachvollziehbar, Auswertungsskript läuft.
```

---

## Phase 8 – Paper-Trading & Journal

```
Baue trading/paper/:
- Orders aus Chart und DOM: Limit, Market, Stop, OCO-Brackets (TP/SL)
- Realistisches Fill-Modell: Limit-Orders stellen sich hinten in die Queue des Levels; gefüllt wird erst, wenn das Volumen vor ihnen abgehandelt bzw. gecancelt wurde (Annahmen dokumentieren). Market-Orders laufen durchs echte Buch (Slippage).
- Gebühren pro Börse konfigurierbar
- Positionen, offene Orders und Breakeven-Linie im Chart
- Trading Journal: jeder Trade mit Zeit, Einstieg, Ausstieg, PnL, Notiz; Export CSV
- Statistik-Panel: Winrate, Avg R, Profit Factor, Max Drawdown, Equity-Kurve
- Funktioniert live und im Replay (Replay = Training)
Abnahme: Paper-Trades in Replay und live möglich, Unit-Tests für Fill-Modell.
```

---

## Phase 9 – Live-Trading (optional, erst nach ausgiebigem Paper-Trading)

```
Baue trading/live/ für Binance Futures und Bybit, ZUERST ausschließlich gegen die jeweiligen Testnets.
Sicherheitsanforderungen (nicht verhandelbar):
- API-Keys nur aus Env/Keyring, Hinweis im README: Keys ohne Withdraw-Recht, mit IP-Whitelist
- Live-Modus muss explizit per Config UND Bestätigung im UI aktiviert werden, dauerhaft sichtbare Warnung im Frontend
- Limits per Config: max. Positionsgröße, max. Order-Größe, max. Tagesverlust → bei Überschreitung keine neuen Orders
- Kill-Switch: ein Klick/Hotkey schließt alle Positionen und cancelt alle Orders
- Lokaler Order-State wird mit dem Börsen-State abgeglichen (User-Data-Stream), bei Abweichung Trading sperren
- Jede Order-Aktion wird geloggt (ohne Secrets)
Gleiche UI wie Paper-Trading, Multi-Account optional.
Abnahme: vollständiger Durchlauf auf Testnet inkl. Kill-Switch-Test.
```

---

## Phase 10 – Feinschliff

```
- Performance-Profiling Backend und Frontend, Engpässe beheben
- Layouts speichern/laden (welche Charts, Indikatoren, Einstellungen)
- Hotkeys
- README: Installation Ubuntu + Windows, Architekturübersicht, wie man einen neuen Adapter oder Indikator hinzufügt
- Startskript, das Backend + Frontend gemeinsam startet
Abnahme: frische Installation auf einer zweiten Maschine nach README klappt.
```
