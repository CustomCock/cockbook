"""Startet den CockBook-MCP-Server aus einem lokalen CockBook-Ordner (nur Standardbibliothek).

Claude Desktop nutzt normalerweise direkt ``uv run …`` aus manifest.json. Dieses Skript ist
der Einstiegspunkt fürs Bundle und ein Ersatz, falls ``uv`` nicht im PATH der App liegt:

    python launcher.py /pfad/zu/cockbook
"""

import os
import shutil
import subprocess
import sys


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("COCKBOOK_DIR", "")
    backend = os.path.join(root, "backend")
    if not os.path.isdir(backend):
        print(f"CockBook-Ordner nicht gefunden: {root!r}", file=sys.stderr)
        return 2
    uv = shutil.which("uv")
    if uv is None:
        print("uv nicht gefunden – https://docs.astral.sh/uv/", file=sys.stderr)
        return 2
    # stdin/stdout werden durchgereicht: darüber läuft das MCP-Protokoll
    cmd = [uv, "run", "--directory", backend, "python", "-m", "cockbook.mcp_server"]
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
