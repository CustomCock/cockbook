"""Anbieter: Kommandozeile bauen, Anmeldestatus prüfen, Ausgabe in einheitliche Ereignisse.

Einheitliche Ereignisse (gehen so ans Frontend):

- ``{"kind": "delta", "text": "…"}``          Text-Stück (live mitschreiben)
- ``{"kind": "text", "text": "…"}``           fertiger Textblock (ersetzt die Deltas davor)
- ``{"kind": "tool", "id", "name", "input"}``  Werkzeugaufruf (z. B. get_orderbook)
- ``{"kind": "tool_result", "id", "ok", "text"}``
- ``{"kind": "session", "id"}``                Sitzungs-ID zum Fortsetzen des Chats
- ``{"kind": "done", "cost_usd"?, "turns"?}`` / ``{"kind": "error", "text"}``

Warum die Programme der Anbieter statt direkt deren API? Die API bräuchte API-Keys mit
eigener Abrechnung; die Programme nutzen dein Abo (Claude Pro/Max bzw. ChatGPT Plus/Pro)
über die offizielle Anmeldung – genau wie die VS-Code-Erweiterungen.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path
from typing import Any

Event = dict[str, Any]

# Variablen, die die Account-Anmeldung aushebeln würden (API-Key statt Abo) oder aus einer
# umgebenden Claude-Code-Sitzung stammen. Das Kindprogramm soll *nur* den Account-Login nutzen.
_STRIP_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "OPENAI_", "CODEX_")
_STRIP_EXACT = {"CLAUDECODE", "CLAUDE_PID", "AZURE_OPENAI_API_KEY"}
_KEEP = {"CLAUDE_CODE_GIT_BASH_PATH", "CODEX_HOME"}


def clean_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for k in list(env):
        if k in _KEEP:
            continue
        if k in _STRIP_EXACT or k.startswith(_STRIP_PREFIXES):
            del env[k]
    return env


def find_executable(name: str, configured: str | None) -> str | None:
    """Pfad aus der Config, sonst PATH, sonst übliche Installationsorte (auch Windows)."""
    if configured:
        p = Path(configured).expanduser()
        return str(p) if p.exists() else None
    found = shutil.which(name)
    if found:
        return found
    home = Path.home()
    exe = ".exe" if sys.platform == "win32" else ""
    candidates = [home / ".local" / "bin" / f"{name}{exe}"]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "npm" / f"{name}.cmd")
    else:
        candidates += [Path("/usr/local/bin") / name, home / ".npm-global" / "bin" / name]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


class Provider(ABC):
    id: str
    label: str
    login_hint: str
    install_hint: str

    def __init__(self, exe: str | None, model: str | None) -> None:
        self.exe = exe
        self.model = model

    @abstractmethod
    def status_cmd(self) -> list[str]: ...

    @abstractmethod
    def parse_status(self, code: int, out: str) -> tuple[bool, str]:
        """(angemeldet und erlaubt?, Anzeige-Text)"""

    @abstractmethod
    def chat_cmd(
        self, prompt: str, session: str | None, new_session: str, mcp: McpSetup
    ) -> list[str]: ...

    @abstractmethod
    def parse_line(self, line: dict[str, Any]) -> Iterable[Event]: ...


class McpSetup:
    """Wie das Kindprogramm den CockBook-MCP-Server startet."""

    def __init__(
        self, config_file: Path, python: str, cwd: Path, env: dict[str, str], tools: list[str]
    ) -> None:
        self.config_file = config_file
        self.python = python
        self.cwd = cwd
        self.env = env
        self.tools = tools  # erlaubte Werkzeuge (ohne Präfix)


# --- Claude (Claude Code) ------------------------------------------------------------------


class ClaudeProvider(Provider):
    """Claude Code im Druck-Modus (``claude -p``), Doku: https://code.claude.com/docs/en/headless

    - Nur die CockBook-Werkzeuge: ``--tools ""`` schaltet Shell/Dateien ab, ``--strict-mcp-config``
      ignoriert andere MCP-Server, ``--allowedTools`` gibt genau unsere frei, ``dontAsk`` lehnt
      alles andere ab (niemand da, der eine Rückfrage beantworten könnte).
    - Anmeldung: ``claude auth status`` (JSON, ``authMethod``). Erlaubt nur Account-Login
      (``claude.ai`` bzw. Abo-Token), nie API-Key – siehe
      https://code.claude.com/docs/en/authentication
    """

    id = "claude"
    label = "Claude"
    login_hint = "Im Terminal einmal `claude` starten und anmelden (Claude Pro/Max)."
    install_hint = (
        "Claude Code installieren: https://code.claude.com/docs/en/setup – "
        "danach `claude` starten und anmelden."
    )
    _ACCOUNT = frozenset({"claude.ai", "oauth_token"})

    def status_cmd(self) -> list[str]:
        return [self.exe or "claude", "auth", "status"]

    def parse_status(self, code: int, out: str) -> tuple[bool, str]:
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return False, "Status unbekannt – Claude Code aktuell? (`claude update`)"
        method = data.get("authMethod")
        if not data.get("loggedIn") or method in (None, "none"):
            return False, "nicht angemeldet"
        if method not in self._ACCOUNT:
            return False, f"angemeldet per {method} – CockBook nutzt nur den Account-Login"
        # E-Mail/Abo nur anzeigen, falls das Programm sie liefert (nicht jede Version tut das)
        extra = [str(data[k]) for k in ("email", "subscriptionType") if data.get(k)]
        return True, " · ".join(["angemeldet (Account)", *extra])

    def chat_cmd(
        self, prompt: str, session: str | None, new_session: str, mcp: McpSetup
    ) -> list[str]:
        cmd = [
            self.exe or "claude", "-p", prompt,
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--mcp-config", str(mcp.config_file), "--strict-mcp-config",
            "--tools", "",
            "--allowedTools", ",".join(f"mcp__cockbook__{t}" for t in mcp.tools),
            "--permission-mode", "dontAsk",
        ]  # fmt: skip
        cmd += ["--resume", session] if session else ["--session-id", new_session]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def parse_line(self, d: dict[str, Any]) -> Iterable[Event]:
        t = d.get("type")
        if t == "system" and d.get("subtype") == "init":
            if d.get("session_id"):
                yield {"kind": "session", "id": d["session_id"]}
            for s in d.get("mcp_servers") or []:
                if s.get("name") == "cockbook" and s.get("status") != "connected":
                    yield {
                        "kind": "error",
                        "text": f"CockBook-Werkzeuge nicht verbunden ({s.get('status')})",
                    }
        elif t == "stream_event":
            e = d.get("event") or {}
            delta = e.get("delta") or {}
            if e.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                yield {"kind": "delta", "text": delta.get("text", "")}
        elif t == "assistant":
            for b in (d.get("message") or {}).get("content") or []:
                if b.get("type") == "text" and b.get("text"):
                    yield {"kind": "text", "text": b["text"]}
                elif b.get("type") == "tool_use":
                    yield {
                        "kind": "tool",
                        "id": b.get("id"),
                        "name": _short(b.get("name", "")),
                        "input": b.get("input"),
                    }
        elif t == "user":
            content = (d.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if b.get("type") == "tool_result":
                    yield {
                        "kind": "tool_result",
                        "id": b.get("tool_use_id"),
                        "ok": not b.get("is_error"),
                        "text": _text_of(b.get("content"))[:2000],
                    }
        elif t == "result":
            if d.get("is_error") or d.get("subtype") not in (None, "success"):
                yield {
                    "kind": "error",
                    "text": str(d.get("result") or d.get("subtype") or "Fehler"),
                }
            yield {"kind": "done", "cost_usd": d.get("total_cost_usd"), "turns": d.get("num_turns")}


# --- ChatGPT (Codex) -----------------------------------------------------------------------


def _toml(v: Any) -> str:
    """Wert für ``-c key=value`` (TOML): Strings/Listen/Tabellen als JSON-kompatibles TOML."""
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k} = {_toml(x)}" for k, x in v.items()) + "}"
    if isinstance(v, bool):
        return "true" if v else "false"
    return json.dumps(v)


class CodexProvider(Provider):
    """Codex CLI (``codex exec --json``), Doku: https://developers.openai.com/codex/noninteractive

    - Nur die CockBook-Werkzeuge: Shell aus (``features.shell_tool=false``), Websuche aus,
      Sandbox read-only, eigene Config des Nutzers ignoriert (keine fremden MCP-Server).
    - ``exec`` fragt nie nach (approval_policy = never) und lehnt MCP-Aufrufe ab, die eine
      Freigabe bräuchten – darum gezielt nur der cockbook-Server ``approval_mode = approve``
      und nur die erlaubten Werkzeuge (``enabled_tools``). https://developers.openai.com/codex/mcp
    - Anmeldung: ``codex login status`` – erlaubt nur "Logged in using ChatGPT".
      https://developers.openai.com/codex/auth
    """

    id = "codex"
    label = "ChatGPT"
    login_hint = "Im Terminal einmal `codex login` ausführen und mit ChatGPT anmelden (Plus/Pro)."
    install_hint = "Codex installieren: `npm install -g @openai/codex` – danach `codex login`."

    def status_cmd(self) -> list[str]:
        return [self.exe or "codex", "login", "status"]

    def parse_status(self, code: int, out: str) -> tuple[bool, str]:
        text = out.strip().splitlines()[-1] if out.strip() else ""
        if "Logged in using ChatGPT" in out:
            return True, "angemeldet (ChatGPT-Account)"
        if code == 0 and "Logged in" in out:
            # API-Key/Token: würde nach Verbrauch abgerechnet, nicht über dein Abo
            return False, "angemeldet per API-Key/Token – CockBook nutzt nur den ChatGPT-Login"
        return False, "nicht angemeldet" if "Not logged in" in out or code == 1 else (
            text or "Status unbekannt"
        )

    def chat_cmd(
        self, prompt: str, session: str | None, new_session: str, mcp: McpSetup
    ) -> list[str]:
        srv = "mcp_servers.cockbook"
        cmd = [
            self.exe or "codex", "exec", "--json", "--skip-git-repo-check", "--ignore-user-config",
            "--sandbox", "read-only", "--color", "never",
            "-c", "features.shell_tool=false",
            "-c", 'web_search="disabled"',
            "-c", f"{srv}.command={_toml(mcp.python)}",
            "-c", f"{srv}.args={_toml(['-m', 'cockbook.mcp_server'])}",
            "-c", f"{srv}.env={_toml(mcp.env)}",
            "-c", f"{srv}.enabled_tools={_toml(mcp.tools)}",
            "-c", f'{srv}.default_tools_approval_mode="approve"',
            "-c", f"{srv}.required=true",
        ]  # fmt: skip
        if self.model:
            cmd += ["--model", self.model]
        # Optionen von exec stehen vor "resume" (Unterbefehl): codex exec [OPT] resume <ID> <PROMPT>
        cmd += ["resume", session, prompt] if session else [prompt]
        return cmd

    def parse_line(self, d: dict[str, Any]) -> Iterable[Event]:
        t = d.get("type")
        raw_item = d.get("item")
        item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else {}
        it = item.get("type")
        if t == "thread.started" and d.get("thread_id"):
            yield {"kind": "session", "id": d["thread_id"]}
        elif t == "item.started" and it == "mcp_tool_call":
            yield {
                "kind": "tool",
                "id": item.get("id"),
                "name": item.get("tool", ""),
                "input": item.get("arguments"),
            }
        elif t == "item.completed" and it == "mcp_tool_call":
            err = item.get("error")
            result = item.get("result") or {}
            text = (
                err.get("message", "") if isinstance(err, dict) else _text_of(result.get("content"))
            )
            ok = item.get("status") == "completed" and not err
            yield {"kind": "tool_result", "id": item.get("id"), "ok": ok, "text": text[:2000]}
        elif t == "item.completed" and it == "agent_message" and item.get("text"):
            yield {"kind": "text", "text": item["text"]}
        elif t == "item.completed" and it == "error":
            yield {"kind": "error", "text": str(item.get("message", "Fehler"))}
        elif t == "turn.failed":
            err = d.get("error") or {}
            yield {"kind": "error", "text": str(err.get("message", "Fehler"))}
            yield {"kind": "done"}
        elif t == "error":
            yield {"kind": "error", "text": str(d.get("message", "Fehler"))}
        elif t == "turn.completed":
            usage = d.get("usage") or {}
            yield {
                "kind": "done",
                "tokens": (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0),
            }


def _short(name: str) -> str:
    return name.removeprefix("mcp__cockbook__")


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return ""
