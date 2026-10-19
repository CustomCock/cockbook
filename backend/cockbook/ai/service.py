"""KI-Panel im Backend: Anfrage aus dem Browser -> Anbieter-Programm starten -> Antwort streamen.

Ablauf einer Nachricht:
1. Browser schickt ``{"type":"ai","op":"send","provider":"claude","chat":"c1","text":…}``
2. Wir starten das Programm des Anbieters (Claude Code bzw. Codex) als Unterprozess, mit dem
   CockBook-MCP-Server als einzigem Werkzeugkasten, und lesen seine JSON-Zeilen.
3. Jede Zeile wird zu einheitlichen Ereignissen (providers.py) und geht als
   ``{"type":"ai_event","chat":"c1","event":{…}}`` an genau diesen Browser-Tab.

Der Chatverlauf liegt beim Anbieter-Programm (Sitzungs-ID); der Browser merkt sich nur die ID
und schickt sie bei der nächsten Nachricht mit. So bleibt CockBook zustandslos.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cockbook.ai.providers import (
    ClaudeProvider,
    CodexProvider,
    McpSetup,
    Provider,
    clean_env,
    find_executable,
)
from cockbook.config import AiConfig

log = logging.getLogger(__name__)

Send = Callable[[dict[str, Any]], None]

# Werkzeuge, die der Chat nutzen darf. Paper-Orders nur, wenn in der Config erlaubt;
# echte Orders gibt es im MCP-Server ohnehin nicht.
READ_DRAW_TOOLS = [
    "list_symbols", "get_orderbook", "get_trade_flow", "get_candles", "get_market_stats",
    "get_paper_account", "get_live_account", "list_drawing_tools", "list_drawings",
    "add_drawing", "update_drawing", "remove_drawing", "clear_drawings",
]  # fmt: skip
PAPER_TOOLS = ["place_paper_order", "close_paper_position", "cancel_paper_orders"]

INSTRUCTIONS = (
    "Du bist der KI-Assistent in CockBook, einer lokalen Orderflow-/Liquiditäts-Heatmap "
    "für Krypto. "
    "Antworte auf Deutsch, knapp und konkret. Nutze die cockbook-Werkzeuge für echte Marktdaten "
    "(Orderbuch, Trade-Flow, Kerzen, Kennzahlen) statt zu raten, und nenne Zahlen mit Einheit. "
    "Zeichnungen erscheinen sofort im Chart des Nutzers: zeichne nur, wenn es hilft oder "
    "gewünscht ist, "
    "und beschrifte sie. Zeiten sind UTC. Echte Orders kannst du nicht platzieren. "
    "Keine Anlageberatung – beschreibe, was die Daten zeigen, und weise auf Unsicherheit hin."
)


class AiService:
    def __init__(
        self,
        cfg: AiConfig,
        *,
        bridge_url: str,
        data_dir: Path,
        python: str = sys.executable,
        providers: dict[str, Provider] | None = None,
    ) -> None:
        self.cfg = cfg
        self.bridge_url = bridge_url
        # absolut: das Kindprogramm läuft in diesem Ordner, relative Pfade zeigten sonst ins Leere
        self.dir = (data_dir / "ai").resolve()
        self.python = python
        self.providers = providers or {
            "claude": ClaudeProvider(find_executable("claude", cfg.claude_path), cfg.claude_model),
            "codex": CodexProvider(find_executable("codex", cfg.codex_path), cfg.codex_model),
        }
        self._runs: dict[str, asyncio.subprocess.Process] = {}

    # --- Hilfen ----------------------------------------------------------------------------

    def _tools(self) -> list[str]:
        return READ_DRAW_TOOLS + (PAPER_TOOLS if self.cfg.allow_paper_orders else [])

    def _mcp(self) -> McpSetup:
        """MCP-Konfiguration schreiben (ohne Geheimnisse: ein Token kommt nur über die Umgebung)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        env = {"COCKBOOK_URL": self.bridge_url}
        conf = {
            "mcpServers": {
                "cockbook": {
                    "type": "stdio",
                    "command": self.python,
                    "args": ["-m", "cockbook.mcp_server"],
                    "env": env,
                }
            }
        }
        path = self.dir / "mcp.json"
        path.write_text(json.dumps(conf, indent=2), encoding="utf-8")
        return McpSetup(path, self.python, self.dir, env, self._tools())

    @staticmethod
    def _prompt(text: str, context: Any, first: bool) -> str:
        parts = []
        if first:
            parts.append(f"[CockBook-Anweisungen] {INSTRUCTIONS}")
        if isinstance(context, dict):
            ctx = {
                k: v
                for k, v in context.items()
                if isinstance(v, (str, int, float)) and len(str(v)) < 200
            }
            if ctx:
                parts.append("[Aktuelle Ansicht im Chart] " + json.dumps(ctx, ensure_ascii=False))
        parts.append(text)
        return "\n\n".join(parts)

    async def _run(self, cmd: list[str], limit_s: float) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=clean_env(),
            cwd=self.dir if self.dir.exists() else None,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), limit_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, "Zeitüberschreitung"
        return proc.returncode or 0, out.decode("utf-8", "replace")

    # --- Anfragen --------------------------------------------------------------------------

    async def handle(self, send: Send, msg: dict[str, Any]) -> None:
        op = msg.get("op")
        if op == "status":
            send({"type": "ai_status", "providers": await self.status()})
        elif op == "send":
            await self.chat(send, msg)
        elif op == "stop":
            proc = self._runs.get(str(msg.get("chat")))
            if proc is not None and proc.returncode is None:
                proc.terminate()

    async def status(self) -> list[dict[str, Any]]:
        self.dir.mkdir(parents=True, exist_ok=True)
        out = []
        for p in self.providers.values():
            row: dict[str, Any] = {"id": p.id, "label": p.label, "installed": p.exe is not None,
                                   "ok": False, "hint": p.login_hint}  # fmt: skip
            if not self.cfg.enabled:
                row["text"] = "KI-Panel ist aus (ai.enabled in config.yaml)"
            elif p.exe is None:
                row["text"] = "nicht installiert"
                row["hint"] = p.install_hint
            else:
                code, text = await self._run(p.status_cmd(), self.cfg.status_timeout_s)
                row["ok"], row["text"] = p.parse_status(code, text)
            out.append(row)
        return out

    async def chat(self, send: Send, msg: dict[str, Any]) -> None:
        chat = str(msg.get("chat") or "")[:64]
        p = self.providers.get(str(msg.get("provider")))
        text = msg.get("text")

        def emit(ev: dict[str, Any]) -> None:
            send({"type": "ai_event", "chat": chat, "event": ev})

        if not self.cfg.enabled or p is None or p.exe is None:
            emit({"kind": "error", "text": "Anbieter nicht verfügbar (installiert? ai.enabled?)"})
            emit({"kind": "done"})
            return
        if not isinstance(text, str) or not text.strip() or len(text) > self.cfg.max_prompt_chars:
            emit(
                {
                    "kind": "error",
                    "text": f"Nachricht leer oder länger als {self.cfg.max_prompt_chars} Zeichen",
                }
            )
            emit({"kind": "done"})
            return
        if chat in self._runs or len(self._runs) >= self.cfg.max_parallel:
            emit(
                {
                    "kind": "error",
                    "text": "Es läuft schon eine Antwort – erst abwarten oder stoppen",
                }
            )
            emit({"kind": "done"})
            return
        session = msg.get("session") if isinstance(msg.get("session"), str) else None
        new_session = str(uuid.uuid4())
        prompt = self._prompt(text, msg.get("context"), first=session is None)
        cmd = p.chat_cmd(prompt, session, new_session, self._mcp())
        log.info("ai chat start", extra={"provider": p.id, "resume": session is not None})
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clean_env(),
                cwd=self.dir,
                limit=32 << 20,  # eine JSON-Zeile kann ein großes Werkzeug-Ergebnis enthalten
            )
        except OSError as e:
            emit({"kind": "error", "text": f"{p.label} ließ sich nicht starten: {e}"})
            emit({"kind": "done"})
            return
        self._runs[chat] = proc
        done = False
        try:
            async with asyncio.timeout(self.cfg.timeout_s):
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    try:
                        line = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(line, dict):
                        continue
                    for ev in p.parse_line(line):
                        done = done or ev["kind"] == "done"
                        emit(ev)
                await proc.wait()
        except TimeoutError:
            proc.kill()
            emit({"kind": "error", "text": f"Abgebrochen nach {self.cfg.timeout_s:.0f} s"})
        finally:
            self._runs.pop(chat, None)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        if not done:
            err = b""
            if proc.stderr is not None:
                with contextlib.suppress(Exception):
                    err = await asyncio.wait_for(proc.stderr.read(4000), 1)
            stopped = proc.returncode in (-15, 143, 1) and not err
            text_err = err.decode("utf-8", "replace").strip()[-600:]
            if not stopped or text_err:
                emit(
                    {
                        "kind": "error",
                        "text": text_err or f"{p.label} beendet (Code {proc.returncode})",
                    }
                )
            emit({"kind": "done"})

    async def close(self) -> None:
        for proc in list(self._runs.values()):
            if proc.returncode is None:
                proc.terminate()
