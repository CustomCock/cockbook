"""Zeichnungen: Prüfung, Speicher je Coin, Bridge-Protokoll, Abgleich mit dem Frontend."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from cockbook.config import AgentConfig
from cockbook.server.drawings import (
    TOOL_POINTS,
    DrawingError,
    DrawingStore,
    drawing_key,
    parse_drawing,
    valid_key,
)

MODEL_TS = Path(__file__).parents[2] / "frontend" / "src" / "drawings" / "model.ts"


def item(**kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": "d1",
        "tool": "trend",
        "pts": [{"t": 1_700_000_000_000, "p": 100.0}, {"t": 1_700_000_060_000, "p": 101.0}],
        "style": {"color": "#5accff", "width": 1.5, "dash": "solid", "fill": None, "text": "",
                  "fontSize": 12, "extendLeft": False, "extendRight": False, "levels": None},
        "created": 1,
    }  # fmt: skip
    d.update(kw)
    return d


def test_tool_list_matches_frontend() -> None:
    src = MODEL_TS.read_text(encoding="utf-8")
    found = dict(re.findall(r'T\("([a-z0-9]+)", "[^"]*", "[a-z]+", (\d+|"free"|"multi")', src))
    frontend = {k: (int(v) if v.isdigit() else v.strip('"')) for k, v in found.items()}
    assert frontend == TOOL_POINTS


def test_keys_are_safe_filenames() -> None:
    assert valid_key("BTCUSDT")
    assert valid_key("1000SHIBUSDT")
    for bad in ("../etc", "a/b", "..", ".hidden", "", "x" * 41, "a\\b", "C:x"):
        assert not valid_key(bad), bad
    assert drawing_key("BITUNIX:BTCUSDT@long") == "BTCUSDT"
    assert drawing_key("MULTI:ETHUSDT") == "ETHUSDT"


def test_parse_rejects_bad_input() -> None:
    lim = AgentConfig()
    assert parse_drawing(item(), lim).tool == "trend"
    with pytest.raises(DrawingError, match="Werkzeug"):
        parse_drawing(item(tool="rm -rf"), lim)
    with pytest.raises(DrawingError, match="2 Punkte"):
        parse_drawing(item(pts=[{"t": 1, "p": 1}]), lim)
    with pytest.raises(DrawingError):
        parse_drawing(item(id="../x"), lim)
    with pytest.raises(DrawingError):
        parse_drawing(item(pts=[{"t": 1, "p": float("nan")}, {"t": 2, "p": 1}]), lim)
    with pytest.raises(DrawingError, match="Text"):
        parse_drawing(item(style={"text": "x" * 5000}), AgentConfig(max_text_len=100))
    # long: im Frontend ein Klick, gespeichert 3 Punkte (Einstieg, Ziel, Stop)
    three = [{"t": 1, "p": 100}, {"t": 2, "p": 101}, {"t": 2, "p": 99}]
    assert len(parse_drawing(item(tool="long", pts=three), lim).pts) == 3
    with pytest.raises(DrawingError, match="mindestens 2"):
        parse_drawing(item(tool="polyline", pts=[{"t": 1, "p": 1}]), lim)


def test_store_put_delete_clear_and_reload(tmp_path: Path) -> None:
    lim = AgentConfig(max_drawings_per_symbol=3)
    st = DrawingStore(tmp_path, lim)
    st.put("BTCUSDT", item(id="a"))
    st.put("BTCUSDT", item(id="b", by="claude"))
    st.put("BTCUSDT", item(id="a", style={"color": "#ff0000"}))  # ersetzt, nicht doppelt
    assert [d["id"] for d in st.get("BTCUSDT")] == ["a", "b"]
    assert st.get("BTCUSDT")[0]["style"]["color"] == "#ff0000"
    st.put("BTCUSDT", item(id="c"))
    with pytest.raises(DrawingError, match="höchstens 3"):
        st.put("BTCUSDT", item(id="d"))
    # neu laden von Platte
    st2 = DrawingStore(tmp_path, lim)
    assert [d["id"] for d in st2.get("BTCUSDT")] == ["a", "b", "c"]
    assert st2.clear("BTCUSDT", by="claude") == 1
    assert [d["id"] for d in st2.get("BTCUSDT")] == ["a", "c"]
    assert st2.delete("BTCUSDT", "a")
    assert not st2.delete("BTCUSDT", "zz")
    assert st2.keys() == ["BTCUSDT"]
    assert json.loads((tmp_path / "BTCUSDT.json").read_text())[0]["id"] == "c"
    with pytest.raises(DrawingError):
        st2.get("../evil")


def test_store_skips_corrupt_entries(tmp_path: Path) -> None:
    (tmp_path / "ETHUSDT.json").write_text(json.dumps([item(id="ok"), {"id": "x", "tool": "?"}]))
    assert [d["id"] for d in DrawingStore(tmp_path, AgentConfig()).get("ETHUSDT")] == ["ok"]
    (tmp_path / "SOLUSDT.json").write_text("{kaputt")
    assert DrawingStore(tmp_path, AgentConfig()).get("SOLUSDT") == []


def test_handle_protocol(tmp_path: Path) -> None:
    st = DrawingStore(tmp_path, AgentConfig())
    reply, everyone = st.handle({"type": "draw", "op": "load", "key": "BTCUSDT"})
    assert reply == {"type": "drawings", "key": "BTCUSDT", "items": []}
    assert not everyone
    reply, everyone = st.handle(
        {"type": "draw", "op": "put", "key": "BTCUSDT", "item": item(), "req": 4}
    )
    assert everyone
    assert reply["req"] == 4
    assert len(reply["items"]) == 1
    reply, everyone = st.handle({"type": "draw", "op": "delete", "key": "BTCUSDT", "id": "nope"})
    assert not everyone
    reply, everyone = st.handle(
        {"type": "draw", "op": "put", "key": "../x", "item": item(), "req": 5}
    )
    assert reply["type"] == "drawings_error"
    assert reply["req"] == 5
    assert not everyone
    reply, _ = st.handle({"type": "draw", "op": "explode", "key": "BTCUSDT"})
    assert reply["type"] == "drawings_error"
