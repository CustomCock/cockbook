"""KI-Panel: Anbieter-Ausgabe übersetzen, Anmeldestatus, Umgebung, Ende-zu-Ende mit Fake-Programmen.

Die Fixtures stammen aus echten Läufen (Claude Code) bzw. dem dokumentierten Format (Codex).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from cockbook.ai.providers import ClaudeProvider, CodexProvider, McpSetup, clean_env
from cockbook.ai.service import PAPER_TOOLS, AiService
from cockbook.config import AiConfig

FIX = Path(__file__).parent / "fixtures"


def events(provider: Any, name: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in (FIX / name).read_text(encoding="utf-8").splitlines():
        out += list(provider.parse_line(json.loads(line)))
    return out


def test_claude_stream_parsing() -> None:
    ev = events(ClaudeProvider("claude", None), "claude_stream.jsonl")
    kinds = [e["kind"] for e in ev]
    assert kinds[0] == "session"
    assert {"kind": "tool", "id": ev[1]["id"], "name": "list_symbols", "input": {}} in ev
    assert any(e["kind"] == "tool_result" and e["ok"] for e in ev)
    assert "".join(e["text"] for e in ev if e["kind"] == "delta") == "Es gibt 13 Symbole."
    assert {"kind": "text", "text": "Es gibt 13 Symbole."} in ev
    assert kinds[-1] == "done"
    assert "error" not in kinds


def test_codex_stream_parsing() -> None:
    ev = events(CodexProvider("codex", None), "codex_exec.jsonl")
    assert ev[0] == {"kind": "session", "id": "0199a213-81c0-7800-8aa1-bbab2a035a53"}
    assert ev[1] == {
        "kind": "tool",
        "id": "item_1",
        "name": "get_orderbook",
        "input": {"symbol": "BTCUSDT"},
    }
    assert ev[2] == {"kind": "tool_result", "id": "item_1", "ok": True, "text": '{"mid": 65000}'}
    assert ev[3] == {"kind": "text", "text": "Der Mid liegt bei 65.000 USDT."}
    assert ev[4]["kind"] == "done"
    failed = list(
        CodexProvider("codex", None).parse_line({"type": "turn.failed", "error": {"message": "x"}})
    )
    assert [e["kind"] for e in failed] == ["error", "done"]


def test_login_status_only_accounts() -> None:
    c = ClaudeProvider("claude", None)
    assert c.parse_status(0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))[0]
    ok, text = c.parse_status(0, json.dumps({"loggedIn": True, "authMethod": "api_key"}))
    assert not ok
    assert "Account" in text
    assert not c.parse_status(1, json.dumps({"loggedIn": False, "authMethod": "none"}))[0]
    x = CodexProvider("codex", None)
    assert x.parse_status(0, "Logged in using ChatGPT\n")[0]
    assert not x.parse_status(0, "Logged in using an API key - sk-…1234\n")[0]
    assert x.parse_status(1, "Not logged in\n") == (False, "nicht angemeldet")


def test_clean_env_removes_api_keys() -> None:
    env = clean_env({"PATH": "/bin", "ANTHROPIC_API_KEY": "k", "CODEX_API_KEY": "k",  # pragma: allowlist secret
                     "OPENAI_API_KEY": "k", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s",  # pragma: allowlist secret
                     "CLAUDE_CODE_GIT_BASH_PATH": "g", "CODEX_HOME": "h"})  # fmt: skip
    assert env == {"PATH": "/bin", "CLAUDE_CODE_GIT_BASH_PATH": "g", "CODEX_HOME": "h"}


def test_commands_restrict_tools(tmp_path: Path) -> None:
    mcp = McpSetup(
        tmp_path / "mcp.json", "/py", tmp_path, {"COCKBOOK_URL": "ws://x"}, ["get_orderbook"]
    )
    c = ClaudeProvider("claude", "opus").chat_cmd("hi", None, "uuid-1", mcp)
    assert c[c.index("--tools") + 1] == ""
    assert c[c.index("--allowedTools") + 1] == "mcp__cockbook__get_orderbook"
    assert "--strict-mcp-config" in c
    assert c[c.index("--session-id") + 1] == "uuid-1"
    assert "--resume" in ClaudeProvider("claude", None).chat_cmd("hi", "s1", "u", mcp)
    x = CodexProvider("codex", None).chat_cmd("hi", "t1", "u", mcp)
    assert "features.shell_tool=false" in x
    assert 'mcp_servers.cockbook.enabled_tools=["get_orderbook"]' in x
    assert x[-3:] == ["resume", "t1", "hi"]
    assert x.index("--json") < x.index("resume")


def fake_cli(tmp_path: Path, name: str, fixture: str, status_out: str, status_code: int) -> str:
    """Skript, das sich wie das Anbieter-Programm verhält und seine Argumente mitschreibt."""
    script = tmp_path / name
    script.write_text(
        f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
open({str(tmp_path / (name + ".args"))!r}, "a").write(json.dumps(args) + "\\n")
if args[:2] in (["auth", "status"], ["login", "status"]):
    sys.stderr.write({status_out!r}) if {name!r} == "codex" else sys.stdout.write({status_out!r})
    sys.exit({status_code})
if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CODEX_API_KEY"):
    sys.exit(9)
sys.stdout.write(open({str(FIX / fixture)!r}).read())
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


async def test_service_end_to_end(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")  # pragma: allowlist secret
    claude = fake_cli(
        tmp_path,
        "claude",
        "claude_stream.jsonl",
        json.dumps({"loggedIn": True, "authMethod": "claude.ai"}),
        0,
    )
    codex = fake_cli(tmp_path, "codex", "codex_exec.jsonl", "Not logged in\n", 1)
    svc = AiService(
        AiConfig(),
        bridge_url="ws://127.0.0.1:1/ws",
        data_dir=tmp_path / "data",
        providers={"claude": ClaudeProvider(claude, None), "codex": CodexProvider(codex, None)},
    )
    sent: list[dict[str, Any]] = []
    await svc.handle(sent.append, {"type": "ai", "op": "status"})
    st = {p["id"]: p for p in sent[-1]["providers"]}
    assert st["claude"]["ok"]
    assert not st["codex"]["ok"]
    assert "codex login" in st["codex"]["hint"]

    sent.clear()
    await svc.handle(sent.append, {"type": "ai", "op": "send", "provider": "claude", "chat": "c1",
                                   "text": "Wie viele Symbole?", "context": {"symbol": "BTCUSDT"}})  # fmt: skip
    kinds = [m["event"]["kind"] for m in sent]
    assert all(m["chat"] == "c1" for m in sent)
    assert kinds[0] == "session"
    assert kinds[-1] == "done"
    assert "error" not in kinds  # API-Key wurde entfernt (sonst Exit 9)
    args = json.loads((tmp_path / "claude.args").read_text().splitlines()[-1])
    prompt = args[args.index("-p") + 1]
    assert "CockBook-Anweisungen" in prompt
    assert "BTCUSDT" in prompt
    assert prompt.endswith("Wie viele Symbole?")
    allowed = args[args.index("--allowedTools") + 1]
    assert not any(t in allowed for t in PAPER_TOOLS)  # Paper-Orders standardmäßig aus
    conf = json.loads((tmp_path / "data" / "ai" / "mcp.json").read_text())
    assert conf["mcpServers"]["cockbook"]["args"] == ["-m", "cockbook.mcp_server"]

    sent.clear()
    await svc.handle(
        sent.append,
        {"op": "send", "provider": "codex", "chat": "c2", "text": "x", "session": "t-9"},
    )
    texts = [m["event"].get("text") for m in sent]
    assert "Der Mid liegt bei 65.000 USDT." in texts
    args = json.loads((tmp_path / "codex.args").read_text().splitlines()[-1])
    assert args[-3:-1] == ["resume", "t-9"]
    assert "CockBook-Anweisungen" not in args[-1]  # nur in der ersten Nachricht


async def test_service_rejects_bad_requests(tmp_path: Path) -> None:
    svc = AiService(AiConfig(), bridge_url="ws://x", data_dir=tmp_path,
                    providers={"claude": ClaudeProvider(None, None)})  # fmt: skip
    sent: list[dict[str, Any]] = []
    await svc.handle(sent.append, {"op": "send", "provider": "claude", "chat": "c", "text": "hi"})
    assert sent[0]["event"]["kind"] == "error"
    assert sent[-1]["event"]["kind"] == "done"
    assert os.listdir(tmp_path) == []
