"""Zeichnungen im Chart (Trendlinien, Fibonacci, Notizen …), gespeichert je Coin.

Das Frontend schickt einzelne Operationen (put/delete/clear/load), der Server antwortet
mit der kompletten Liste des Coins – an *alle* verbundenen Clients. Dadurch sehen mehrere
Browser-Tabs dieselben Zeichnungen, und Zeichnungen, die Claude über die MCP-Erweiterung
setzt, erscheinen sofort im Chart.

Warum validieren, obwohl nur das eigene Frontend schreibt? Der Schlüssel landet im
Dateinamen (``data/drawings/<key>.json``) – ein ``../`` darin wäre ein Pfad-Ausbruch –, und
die MCP-Erweiterung reicht Eingaben eines Sprachmodells durch. Also: Whitelist für
Werkzeuge, feste Grenzen für Anzahl, Punkte und Textlänge.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cockbook.config import AgentConfig

log = logging.getLogger(__name__)

# Werkzeug -> Anzahl Ankerpunkte (Zahl = fest, "free"/"multi" = beliebig viele).
# Muss zu frontend/src/drawings/model.ts passen (Test: test_drawings.py prüft das).
# long/short: Einstieg, Ziel, Stop (3 Punkte; im Frontend reicht ein Klick).
TOOL_POINTS: dict[str, int | str] = {
    "trend": 2, "ray": 2, "infoline": 2, "extended": 2, "trendangle": 2,
    "hline": 1, "hray": 1, "vline": 1, "crossline": 1,
    "parallel": 3, "regression": 2, "flattop": 3, "disjoint": 4,
    "pitchfork": 3, "schiff": 3, "modschiff": 3, "insidefork": 3,
    "fibret": 2, "fibext": 3, "fibchannel": 3, "fibtimezone": 2, "fibfan": 2, "fibtime": 3,
    "fibcircles": 2, "fibspiral": 2, "fibarcs": 2, "fibwedge": 3, "pitchfan": 3,
    "gannbox": 2, "gannsqfixed": 1, "gannsq": 2, "gannfan": 2,
    "xabcd": 5, "cypher": 5, "headshoulders": 7, "abcd": 4, "trianglepattern": 4,
    "threedrives": 7, "elliottimpulse": 6, "elliottcorrection": 4, "elliotttriangle": 6,
    "elliottdouble": 4, "elliotttriple": 6, "cycliclines": 2, "timecycles": 2, "sine": 2,
    "long": 1, "short": 1, "forecast": 2, "barspattern": 3, "ghostfeed": "multi",
    "sector": 3, "avwap": 1, "fixedvp": 2, "anchoredvp": 1,
    "pricerange": 2, "daterange": 2, "dateprice": 2,
    "rect": 2, "rotrect": 3, "path": "multi", "circle": 2, "ellipse": 2, "polyline": "multi",
    "triangle": 3, "arc": 3, "curve": 3, "doublecurve": 4,
    "brush": "free", "highlighter": "free",
    "arrow": 2, "arrowmarker": 2, "arrowup": 1, "arrowdown": 1, "arrowleft": 1, "arrowright": 1,
    "text": 1, "note": 1, "pricenote": 2, "pin": 1, "table": 1, "callout": 2, "comment": 1,
    "pricelabel": 1, "signpost": 1, "icon": 1,
}  # fmt: skip

# Ein-Klick-Werkzeuge, die gespeichert mehr Punkte haben (Frontend: expandSinglePoint)
EXPANDED_POINTS: dict[str, int] = {"long": 3, "short": 3, "gannsqfixed": 2}

# Coin-Schlüssel (z. B. "BTCUSDT", "1000SHIBUSDT"): wird Dateiname -> streng
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_COLOR_MAX = 40


def valid_key(key: object) -> bool:
    return isinstance(key, str) and _KEY_RE.fullmatch(key) is not None and ".." not in key


def drawing_key(symbol: str) -> str:
    """Wie ``drawingKey`` im Frontend: Börse und Ansicht abschneiden (``BITUNIX:BTCUSDT@long``)."""
    return symbol.split(":", 1)[-1].split("@", 1)[0]


def _finite(v: float) -> float:
    if not math.isfinite(v):
        raise ValueError("Zahl muss endlich sein")
    return v


class _M(BaseModel):
    # Unbekannte Felder still verwerfen: neueres Frontend darf mehr schicken
    model_config = ConfigDict(extra="ignore")


class Point(_M):
    t: float  # ms UTC
    p: float  # Preis

    @field_validator("t", "p")
    @classmethod
    def _fin(cls, v: float) -> float:
        return _finite(v)


class Style(_M):
    color: Annotated[str, Field(max_length=_COLOR_MAX)] = "#5accff"
    width: Annotated[float, Field(ge=0.5, le=12)] = 1.5
    dash: Literal["solid", "dashed", "dotted"] = "solid"
    fill: Annotated[str, Field(max_length=_COLOR_MAX)] | None = None
    text: str = ""
    fontSize: Annotated[float, Field(ge=6, le=96)] = 12  # noqa: N815 – Feldname wie im Frontend
    extendLeft: bool = False  # noqa: N815
    extendRight: bool = False  # noqa: N815
    levels: Annotated[list[float], Field(max_length=40)] | None = None

    @field_validator("levels")
    @classmethod
    def _lv(cls, v: list[float] | None) -> list[float] | None:
        return None if v is None else [_finite(x) for x in v]


class Drawing(_M):
    id: str
    tool: str
    pts: list[Point]
    style: Style = Field(default_factory=Style)
    locked: bool = False
    hidden: bool = False
    by: Literal["user", "claude"] = "user"
    created: int = 0

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID_RE.fullmatch(v):
            raise ValueError("ungültige id")
        return v

    @field_validator("tool")
    @classmethod
    def _tool(cls, v: str) -> str:
        if v not in TOOL_POINTS:
            raise ValueError(f"unbekanntes Werkzeug: {v}")
        return v


class DrawingError(ValueError):
    pass


def parse_drawing(raw: object, limits: AgentConfig) -> Drawing:
    """Prüft eine Zeichnung (Schema, Werkzeug, Punktanzahl, Textlänge)."""
    try:
        d = Drawing.model_validate(raw)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(x) for x in first["loc"])
        raise DrawingError(f"ungültige Zeichnung ({loc}: {first['msg']})") from e
    spec = TOOL_POINTS[d.tool]
    n = len(d.pts)
    if n == 0 or n > limits.max_points_per_drawing:
        raise DrawingError(f"{d.tool}: 1 bis {limits.max_points_per_drawing} Punkte erlaubt")
    if isinstance(spec, int):
        allowed = {spec, EXPANDED_POINTS.get(d.tool, spec)}
        if n not in allowed:
            want = " oder ".join(str(x) for x in sorted(allowed))
            raise DrawingError(f"{d.tool} braucht {want} Punkte, nicht {n}")
    elif spec == "multi" and n < 2:
        raise DrawingError(f"{d.tool} braucht mindestens 2 Punkte")
    if len(d.style.text) > limits.max_text_len:
        raise DrawingError(f"Text länger als {limits.max_text_len} Zeichen")
    return d


class DrawingStore:
    """Zeichnungen je Coin, im Speicher + ``<dir>/<key>.json`` (atomar geschrieben)."""

    def __init__(self, directory: Path, limits: AgentConfig) -> None:
        self.dir = directory
        self.limits = limits
        self._items: dict[str, list[dict[str, Any]]] = {}

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> list[dict[str, Any]]:
        if not valid_key(key):
            raise DrawingError(f"ungültiger Schlüssel: {key!r}")
        items = self._items.get(key)
        if items is None:
            items = self._load(key)
            self._items[key] = items
        return items

    def keys(self) -> list[str]:
        on_disk = {p.stem for p in self.dir.glob("*.json")} if self.dir.exists() else set()
        in_memory = {k for k, v in self._items.items() if v}
        return sorted(in_memory | {k for k in on_disk if valid_key(k)})

    def _load(self, key: str) -> list[dict[str, Any]]:
        path = self._path(key)
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("drawings file unreadable, starting empty", extra={"key": key})
            return []
        out: list[dict[str, Any]] = []
        for r in raw if isinstance(raw, list) else []:
            try:
                out.append(parse_drawing(r, self.limits).model_dump())
            except DrawingError:
                continue  # einzelne kaputte Zeichnung überspringen, Rest behalten
        return out

    def _save(self, key: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        text = json.dumps(self._items.get(key, []), separators=(",", ":"))
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def put(self, key: str, raw: object) -> dict[str, Any]:
        """Neu oder ersetzen (gleiche id). Rückgabe: gespeicherte Form."""
        d = parse_drawing(raw, self.limits).model_dump()
        items = self.get(key)
        for i, x in enumerate(items):
            if x["id"] == d["id"]:
                items[i] = d
                break
        else:
            if len(items) >= self.limits.max_drawings_per_symbol:
                n = self.limits.max_drawings_per_symbol
                raise DrawingError(f"höchstens {n} Zeichnungen je Coin")
            items.append(d)
        self._save(key)
        return d

    def delete(self, key: str, drawing_id: str) -> bool:
        items = self.get(key)
        keep = [x for x in items if x["id"] != drawing_id]
        if len(keep) == len(items):
            return False
        self._items[key] = keep
        self._save(key)
        return True

    def clear(self, key: str, by: str | None = None) -> int:
        """Alle löschen – oder nur die eines Urhebers (z. B. by="claude")."""
        items = self.get(key)
        keep = [x for x in items if by is not None and x.get("by") != by]
        removed = len(items) - len(keep)
        if removed:
            self._items[key] = keep
            self._save(key)
        return removed

    # --- Bridge-Protokoll ------------------------------------------------------------------

    def handle(self, msg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Steuer-Nachricht ``{"type":"draw","op":…,"key":…}``.

        Rückgabe: (Antwort, an alle senden?). Änderungen gehen an alle Clients, ``load``
        nur an den Anfragenden.
        """
        op = msg.get("op")
        key = msg.get("key")
        req = msg.get("req")
        try:
            if not isinstance(key, str):
                raise DrawingError("key fehlt")
            changed = True
            if op == "load":
                changed = False
            elif op == "put":
                self.put(key, msg.get("item"))
            elif op == "delete":
                did = msg.get("id")
                changed = isinstance(did, str) and self.delete(key, did)
            elif op == "clear":
                by = msg.get("by")
                changed = self.clear(key, by if isinstance(by, str) else None) > 0
            else:
                raise DrawingError(f"unbekannte Operation: {op}")
            reply = {"type": "drawings", "key": key, "items": self.get(key)}
        except DrawingError as e:
            return {"type": "drawings_error", "key": key, "req": req, "message": str(e)}, False
        if req is not None:
            reply["req"] = req
        return reply, changed
