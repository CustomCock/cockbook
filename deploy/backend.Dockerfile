# Backend: Python + uv. Läuft auf x86 (AMD) und ARM (Ampere) gleichermaßen.
FROM python:3.13-slim

# uv als statisches Binary aus dem offiziellen Image (feste Version = reproduzierbar)
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app/backend
# Erst nur die Abhängigkeiten (Docker-Cache: ändert sich selten)
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev

# Daten (Langzeit-Heatmap, Tageskerzen-Cache) landen unter /app/data -> Volume
WORKDIR /app
RUN useradd --system --uid 10001 cockbook && mkdir -p /app/data && chown cockbook /app/data
USER cockbook

# Server-Betrieb: lauscht im Container auf allen Schnittstellen, braucht COCKBOOK_TOKEN
ENV COCKBOOK_PUBLIC=1
EXPOSE 8765
CMD ["/app/backend/.venv/bin/python", "-m", "cockbook", "--config", "/app/config.yaml"]
