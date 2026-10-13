# CockBook

> **Status: Work in Progress** – aktuell Phase 0 (Grundgerüst). Noch keine Live-Daten, keine Heatmap.

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

Voraussetzungen: [uv](https://docs.astral.sh/uv/), Node.js ≥ 20, `make` (Windows: z. B. über Git Bash).

```bash
make install   # Backend (uv sync) + Frontend (npm install)
make hooks     # Pre-Commit-Hooks inkl. Secret-Scan aktivieren
make test      # pytest
make lint      # ruff, mypy, tsc
make dev       # Backend + Frontend-Dev-Server (http://127.0.0.1:5173)
```

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
