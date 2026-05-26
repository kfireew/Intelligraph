# Kfirs-Intelliscan — Codebase Graph Q&A Chat

A pod that serves a web UI where users upload knowledge graphs of their codebase and chat with them. Everything runs in the browser — no server-side graph storage.

## Architecture

```
User's machine                    Pod (thin server)
┌──────────────────────┐         ┌─────────────────────────┐
│ graph-builder.exe    │         │ /auth/login (OIDC)      │
│  → graph.json + .db  │         │ /auth/callback          │
│                      │         │ /auth/me /auth/logout   │
│ Browser (IndexedDB)  │         │                         │
│  graph.json + sql.js │         │ /llm/relay (proxy)      │
│  Intent detection    │──LLM───►│ /mcp/message (optional) │
│  Client-side queries │         │                         │
│  Chat UI             │         │ /download/mcp-server    │
└──────────────────────┘         │ /download/graph-builder │
                                 └─────────────────────────┘
```

**Key: graphs never leave the user's browser** (unless they explicitly enable online MCP).

## Quick Start

```bash
docker build -t graphify-qa .
docker run -p 5050:5050 graphify-qa
# Open http://localhost:5050
```

With OIDC:
```bash
docker run -p 5050:5050 \
  -e OIDC_ISSUER=https://sso.internal \
  -e OIDC_CLIENT_ID=graphify-qa \
  -e OIDC_CLIENT_SECRET=secret \
  graphify-qa
```

## User Workflow

1. Download `graph-builder.exe` from the "How to Generate" tab
2. Run `graph-builder.exe C:\your-project` (creates graph.json + graph.db)
3. Upload both files in the "Upload Graph" tab (stays in browser IndexedDB)
4. Chat in the "Chat" tab — ask architecture, callers, impact, tests
5. Optionally configure LLM in "LLM Settings" for natural-language answers

## GUI Tabs

| Tab | Purpose |
|-----|---------|
| **Chat** | Query your codebase. Ask anything. |
| **Upload Graph** | Drag-and-drop graph.json + graph.db. Stored in IndexedDB. |
| **LLM Settings** | Configure your LLM endpoint/token. Stored in localStorage. |
| **How to Generate** | Download graph-builder.exe, MCP server, instructions. |

## MCP Server for Claude Code

### Option A: Download standalone script

From the "How to Generate" tab, download `mcp_server_standalone.py`. Run locally:

```bash
python mcp_server_standalone.py --crg-db .code-review-graph/graph.db --graphify graphify-out/graph.json
```

Claude Code `.mcp.json`:
```json
{
  "mcpServers": {
    "graphify-qa": {
      "command": "python",
      "args": ["mcp_server_standalone.py", "--crg-db", ".code-review-graph/graph.db", "--graphify", "graphify-out/graph.json"],
      "cwd": "/path/to/your/project"
    }
  }
}
```

### Option B: Online MCP (upload to pod)

Upload your graph files in the "How to Generate" tab → pod serves `/mcp/message`.

## Building graph-builder.exe (dev only)

```bash
pip install pyinstaller graphifyy code-review-graph
pyinstaller graph_builder.spec
# Output: dist/graph-builder.exe
# Copy to downloads/ folder in the pod
```

## Files

| File | Purpose |
|------|---------|
| `app.py` | Thin Flask server (OIDC + LLM relay + MCP + downloads) |
| `mcp_server.py` | MCP Blueprint for online mode |
| `mcp_server_standalone.py` | Downloadable standalone MCP server |
| `graph_builder.py` | PyInstaller wrapper for building graphs |
| `graph_builder.spec` | PyInstaller build spec |
| `Dockerfile` | Container image |
| `templates/index.html` | Full client-side app |
| `wheels/` | Offline pip packages (Flask + requests only) |

~1200 lines total. No database required at runtime.