#!/usr/bin/env bash
# MongoDB's official MCP server (pinned 3.0.5) for this project, in READ-ONLY mode.
# - The connection string is read from .env at start-up: it never appears in .mcp.json, CLI args or process lists.
# - --readOnly / MDB_MCP_READ_ONLY: create/update/delete tools are not registered at all.
# - Telemetry disabled.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NPX="$(command -v npx || true)"
[ -n "$NPX" ] || NPX="$(ls -d "$HOME"/.nvm/versions/node/*/bin/npx 2>/dev/null | tail -1)"
[ -n "$NPX" ] || { echo "npx not found; install Node.js 20+" >&2; exit 1; }
export PATH="$(dirname "$NPX"):$PATH"
MDB_MCP_CONNECTION_STRING="$("$ROOT/.venv/bin/python" -c '
import sys
from dotenv import dotenv_values
v = dotenv_values(sys.argv[1]).get("MONGODB_URI")
sys.exit("MONGODB_URI missing in .env") if not v else print(v)' "$ROOT/.env")"
export MDB_MCP_CONNECTION_STRING
export MDB_MCP_READ_ONLY=true MDB_MCP_TELEMETRY=disabled DO_NOT_TRACK=1
exec "$NPX" -y mongodb-mcp-server@3.0.5 --readOnly
