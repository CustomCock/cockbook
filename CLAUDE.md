# CockBook – Orderflow- & Liquidity-Heatmap für Krypto

Eigenes, kostenloses Bookmap-artiges Tool für Krypto-Märkte. Nur öffentliche, kostenlose Börsen-Datenfeeds. Läuft lokal unter Ubuntu und Windows.

## Ziel
Funktionsumfang orientiert an Bookmap Global+, beschränkt auf Krypto:
Liquidity-Heatmap, Trades/Volume-Bubbles, DOM, Time & Sales, CVD, Alerts, Record & Replay, Multibook über mehrere Börsen, Orderflow-Indikatoren (Absorption, Sweeps, Icebergs, Imbalance …), Paper-Trading, später optional Live-Trading über Exchange-APIs.

## Architektur (verbindlich)
- **Backend:** Python 3.12+, `asyncio`. Paketverwaltung mit `uv`. Lint/Format mit `ruff`, Typen mit `mypy` (strict für `core/`), Tests mit `pytest` + `pytest-asyncio`.
- **Frontend:** TypeScript + Vite, Rendering über Canvas2D/WebGL (Heatmap als Textur-Ringpuffer). Kein schweres UI-Framework für den Chart selbst; für Panels/Settings ist ein leichtes Framework (z. B. Svelte oder Preact) okay.
- **Bridge:** Backend stellt einen lokalen WebSocket-Server (127.0.0.1) bereit; Frontend verbindet sich dorthin. Binäres Format (MessagePack) für Heatmap-Frames, JSON nur für Steuer-Nachrichten.
- **Datenquellen-Abstraktion:** Live-Feed und Replay implementieren dasselbe Interface. Alles hinter dem Adapter (Orderbuch, Indikatoren, Frontend) darf nicht wissen, ob live oder Replay läuft.
- **Normalisiertes Datenmodell:** Jeder Adapter übersetzt börsenspezifische Nachrichten in gemeinsame Events: `BookSnapshot`, `BookDelta`, `Trade` (mit Aggressor-Seite), `Liquidation`, `L3Order` (nur wo verfügbar). Timestamps intern als int Nanosekunden, zusätzlich Exchange-Timestamp und lokaler Empfangszeitpunkt.
- **Preise/Mengen:** Niemals float für Orderbuch-Keys. Preise als int in Tick-Einheiten (Tick-Größe pro Symbol aus der Exchange-Info), Mengen als `Decimal` oder skalierte int.

## Projektstruktur
```
cockbook/
  backend/
    cockbook/
      adapters/      # binance.py, bybit.py, okx.py, coinbase.py ...
      core/          # events, orderbook, aggregation, eventbus
      recorder/      # Rohdaten-Aufzeichnung
      replay/        # Replay-Datenquelle
      indicators/    # je Indikator ein Modul
      trading/       # paper/, live/
      server/        # WebSocket-Bridge zum Frontend
      config.py
    tests/
  frontend/
    src/
  config.yaml
  data/              # Aufzeichnungen (gitignored)
```

## Regeln für die Arbeit
- **Aktuelle API-Dokumentation lesen, nicht aus dem Gedächtnis arbeiten.** Endpoints, Stream-Namen, Rate-Limits und Sync-Prozeduren der Börsen ändern sich. Vor jedem Adapter die offizielle Doku abrufen und die relevante Prozedur im Code-Kommentar mit Link referenzieren.
- **Orderbuch-Korrektheit vor allem anderen.** Snapshot + gepufferte Deltas + Sequenz-/Update-ID-Prüfung exakt nach Börsen-Doku. Bei Lücke: verwerfen und neu synchronisieren, nie still weiterlaufen. Wo die Börse Checksummen liefert, diese prüfen.
- **Robustheit:** automatischer Reconnect mit Backoff, Heartbeat/Ping, sauberes Herunterfahren, keine unbehandelten Task-Exceptions.
- **Tests:** Orderbuch-Logik und jeder Indikator bekommen Unit-Tests mit festen Fixtures. Heuristiken (Absorption, Sweeps, Icebergs) werden gegen aufgezeichnete Replay-Daten validiert.
- **Konfiguration** über `config.yaml` (Symbole, Börsen, Aggregationsintervalle, Schwellwerte). Keine hartkodierten Magic Numbers in Indikatoren.
- **Secrets:** API-Keys nur aus Umgebungsvariablen oder OS-Keyring, niemals in Dateien im Repo, niemals loggen.
- **Erklären:** Bei nicht-trivialen Designentscheidungen (Datenstrukturen, Sync-Logik, Rendering-Ansatz) kurz im Chat begründen, warum diese Lösung und nicht die naheliegende Alternative. Der Nutzer will das System verstehen, nicht nur benutzen.
- **Öffentliches Repo (GitHub, Account CustomCock).** Alles, was committet wird, ist öffentlich:
  - Keine Secrets, keine Aufzeichnungen aus `data/`, keine persönlichen Pfade oder Mail-Adressen im Code.
  - Keine Bookmap-Assets: kein Logo, keine Screenshots, keine kopierten Texte, Icons oder Farbschemata 1:1. Eigene Optik.
  - „Bookmap" nur beschreibend erwähnen („inspiriert von Bookmap"), nie im Namen, in Paket-/Modulnamen oder so, dass eine Verbindung zu Bookmap Ltd. impliziert wird.
  - README enthält einen Hinweis: kein offizielles Produkt, nicht mit Bookmap Ltd. verbunden; Bookmap ist eine Marke der Bookmap Ltd.
  - README enthält einen Risiko-Hinweis: keine Anlageberatung, Nutzung auf eigenes Risiko, Live-Trading kann zu Verlusten führen.
  - Lizenz: MIT (LICENSE-Datei im Root).
- **Echte Daten statt Testdaten für den Nutzer.** Der Nutzer probiert alles immer mit echten Binance-Daten (bzw. echten Börsendaten). Der Simulator (`backend/cockbook/sim/`) und Fake-Server sind nur für Claudes interne Tests (z. B. wenn der Container Binance nicht erreicht) – nie in README, Makefile oder Startanleitungen für den Nutzer anbieten. Abnahmen am Phasenende beschreiben, wie der Nutzer sie mit Live-Daten prüft.
- Am Ende jeder Phase: Tests laufen lassen, kurz zusammenfassen was gebaut wurde, was bekannt nicht funktioniert und wie man es startet. Dann stoppen und auf die nächste Phase warten.
