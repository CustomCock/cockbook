# CockBook – Entwickler-Kommandos. Unter Windows z. B. via Git Bash + make
# (oder die Befehle direkt ausführen, siehe README).

UV      ?= uv
NPM     ?= npm
BACKEND := backend
FRONTEND:= frontend
CONFIG  ?= config.yaml
PY      := $(UV) run --project $(BACKEND)

.PHONY: help install hooks dev dev-backend dev-frontend test lint format record replay

help:
	@echo "install  – Backend- und Frontend-Abhängigkeiten installieren"
	@echo "hooks    – Pre-Commit-Hooks (Secret-Scan, ruff) installieren"
	@echo "dev      – Backend + Frontend-Dev-Server parallel starten"
	@echo "test     – Backend-Tests (pytest)"
	@echo "lint     – ruff, mypy, tsc"
	@echo "format   – Code formatieren (ruff)"
	@echo "record   – Recorder starten (Phase 1)"
	@echo "replay   – Replay starten (Phase 4)"

install:
	cd $(BACKEND) && $(UV) sync
	cd $(FRONTEND) && $(NPM) install

hooks:
	$(PY) pre-commit install

dev:
	$(MAKE) -j2 dev-backend dev-frontend

dev-backend:
	$(PY) python -m cockbook --config $(CONFIG)

dev-frontend:
	cd $(FRONTEND) && $(NPM) run dev

test:
	cd $(BACKEND) && $(UV) run pytest

lint:
	cd $(BACKEND) && $(UV) run ruff check . && $(UV) run ruff format --check . && $(UV) run mypy cockbook tests
	cd $(FRONTEND) && $(NPM) run typecheck

format:
	cd $(BACKEND) && $(UV) run ruff format . && $(UV) run ruff check --fix .

record:
	@echo "Recorder ist noch nicht implementiert (Phase 1)." && exit 1

replay:
	@echo "Replay ist noch nicht implementiert (Phase 4)." && exit 1
