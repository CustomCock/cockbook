# CockBook – Entwickler-Kommandos. Unter Windows z. B. via Git Bash + make
# (oder die Befehle direkt ausführen, siehe README).

UV      ?= uv
NPM     ?= npm
BACKEND := backend
FRONTEND:= frontend
CONFIG  ?= config.yaml
PY      := $(UV) run --project $(BACKEND)

.PHONY: help check-node install hooks dev dev-backend dev-frontend test lint format record book replay

help:
	@echo "install  – Backend- und Frontend-Abhängigkeiten installieren"
	@echo "hooks    – Pre-Commit-Hooks (Secret-Scan, ruff) installieren"
	@echo "dev      – Backend + Frontend-Dev-Server parallel starten"
	@echo "test     – Tests (pytest + vitest)"
	@echo "lint     – ruff, mypy, tsc"
	@echo "format   – Code formatieren (ruff)"
	@echo "record   – Recorder starten (schreibt nach data/)"
	@echo "book     – Live-Orderbuch im Terminal (debug_book)"
	@echo "replay   – Replay starten (Phase 4)"

# Klare Fehlermeldung statt "Fehler 127", wenn Node/npm fehlen oder zu alt sind
# (Vite 7 / Vitest 5 brauchen Node >= 22.12).
check-node:
	@command -v node >/dev/null 2>&1 && command -v $(NPM) >/dev/null 2>&1 || { \
	  echo "Node.js/npm nicht gefunden. Installation siehe README (Abschnitt Voraussetzungen)."; exit 1; }
	@node $(FRONTEND)/scripts/check-node.mjs

install: check-node
	cd $(BACKEND) && $(UV) sync
	cd $(FRONTEND) && $(NPM) install

hooks:
	$(PY) pre-commit install

dev:
	$(MAKE) -j2 dev-backend dev-frontend

dev-backend:
	$(PY) python -m cockbook --config $(CONFIG)

dev-frontend: check-node
	cd $(FRONTEND) && { test -d node_modules || $(NPM) install; } && $(NPM) run dev

test:
	cd $(BACKEND) && $(UV) run pytest
	cd $(FRONTEND) && $(NPM) test

lint:
	cd $(BACKEND) && $(UV) run ruff check . && $(UV) run ruff format --check . && $(UV) run mypy cockbook tests
	cd $(FRONTEND) && $(NPM) run typecheck

format:
	cd $(BACKEND) && $(UV) run ruff format . && $(UV) run ruff check --fix .

record:
	$(PY) python -m cockbook.recorder --config $(CONFIG)

book:
	$(PY) python -m cockbook.debug_book --config $(CONFIG)

replay:
	@echo "Replay ist noch nicht implementiert (Phase 4)." && exit 1
