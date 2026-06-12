# Intelligraph

Codebase intelligence platform — clone repos, index them with graphify + code-review-graph, and chat with an LLM about architecture, callers, callees, impact, and test coverage.

<img width="1774" height="887" alt="ChatGPT Image Jun 4, 2026, 01_33_41 AM (1)" src="https://github.com/user-attachments/assets/7ba01d69-56b1-4915-a846-13372519188f" />

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Browser (React)                          │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────────┐ │
│  │ Sidebar  │ │ChatPanel │ │GraphPanel│ │ UploadPanel/Guide  │ │
│  └──────────┘ └──────────┘ └──────────┘ └────────────────────┘ │
│       │              │            │              │               │
│       └──────────────┴────────────┴──────────────┘               │
│                        │ SSE + REST                              │
└────────────────────────┼────────────────────────────────────────┘
                         │
┌────────────────────────┼────────────────────────────────────────┐
│                   Flask Backend (app.py)                         │
│  ┌───────────┐  ┌──────────────┐  ┌───────────┐  ┌───────────┐ │
│  │ /clone    │  │ /chat-context│  │ /graph-html│  │ /llm/relay│ │
│  └───────────┘  └──────────────┘  └───────────┘  └───────────┘ │
│       │               │                                    │     │
│       ▼               ▼                                    ▼     │
│  ┌──────────┐  ┌──────────────┐              ┌─────────────┐   │
│  │ graphify │  │ CRG SQLite   │              │ LLM Relay   │   │
│  │ update   │  │ (FTS5)       │              │ (openrouter)│   │
│  └──────────┘  └──────────────┘              └─────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## Stack

| Layer | Tech |
|-------|------|
| Frontend | React 18 + Vite 6 + Tailwind CSS v4 + Framer Motion |
| Backend | Flask (Python) with OIDC auth |
| Graph engine | [graphify](https://github.com/danielma-sifry/graphify) — code structure graph |
| Dep graph | [code-review-graph](https://github.com/ahsolani/code-review-graph) — dependency graph with FTS5 |
| Persistence | Server-side SQLite (`intelligraph.db`) + client-side IndexedDB |
| LLM relay | SSE streaming through any OpenAI-compatible API |

## How it works

### 1. Clone / Upload

**Git clone** — User provides a Bitbucket/GitHub URL. Backend runs:

1. `git clone --depth 1` into a temp directory
2. `graphify update .` — parses the codebase, generates `graphify-out/graph.json` (nodes, edges, communities, code chunks)
3. `code-review-graph build` — Tree-sitter parse, generates `.code-review-graph/graph.db` (SQLite with FTS5 full-text search)
4. `graphify.export.to_html()` — generates `graphify-out/graph.html` (vis-network visualization)
5. Stores metadata in `intelligraph.db`

**Manual upload** — User provides `graph.json` + `graph.db` + `graph.html` via the New Project → Upload tab. Files are stored server-side and synced to IndexedDB for offline access.

### 2. Chat with context

When the user sends a prompt, context is built in 3 stages:

**Stage 1 — Client-side CRG (IndexedDB)**
The frontend queries the CRG SQLite database (loaded via sql.js) with 8 FTS5 helpers:
- `searchNodes` — name/qualified-name search
- `callers` / `callees` — dependency lookup
- `impact` — blast radius analysis
- `architecture` — community overview
- `tests` — test coverage

**Stage 2 — graphify semantic search (server)**
Two API calls to the graphify graph:
- `graphify query` — semantic search over code structure
- `graphify explain` — plain-language analysis of a concept

**Stage 3 — Server context (ChromaDB/lexical)**
The backend runs its own context builder with additional heuristics and fallback file injection.

The combined context (capped at 8,000 characters) is prepended to the user's prompt and sent to the LLM.

### 3. LLM relay (SSE)

The backend proxies the prompt to any OpenAI-compatible API (OpenRouter by default) and streams tokens back as Server-Sent Events:

```
Client                    Backend                    LLM API
  │                          │                          │
  │── POST /chat-context ───▶│                          │
  │◀──── context JSON ───────│                          │
  │                          │                          │
  │── POST /llm/relay/stream─▶│                          │
  │                          │── POST /chat/completions─▶│
  │◀─── SSE: text chunk ─────│◀─── SSE: text chunk ──────│
  │◀─── SSE: text chunk ─────│◀─── SSE: text chunk ──────│
  │◀─── SSE: done ───────────│◀─── [stream ends] ───────│
```

The frontend replaces em dashes and other mojibake from the stream before rendering.

### 4. Graph visualization

`GraphPanel` embeds the graphify-generated `graph.html` in an iframe. The backend injects a dark theme CSS override. The panel supports:
- Full-screen expand/collapse
- Sidebar collapse (animates width → 0, iframe stays mounted)
- React Flow node selection and detail pane

## Project structure

```
Kfirs-Intelligraph/
├── backend/
│   ├── app.py              # Flask application — all endpoints
│   ├── mcp_server.py       # MCP server (HTTP)
│   ├── graph_builder.py    # PyInstaller wrapper — generates graph.json + graph.db + graph.html
│   ├── templates/          # Jinja templates
│   ├── static/             # Static assets
│   ├── tests/              # pytest suite (54 tests)
│   │   ├── test_app.py     # API endpoint tests
│   │   ├── test_mcp.py     # MCP server tests
│   │   └── test_graph_builder.py
│   └── requirements.txt
├── src/
│   ├── App.jsx             # Root — wires hooks, panel switching, graph collapse state
│   ├── components/
│   │   ├── ChatPanel.jsx   # Chat UI with conversation sidebar + graph toggle
│   │   ├── ChatMessage.jsx # Renders markdown messages with route badges
│   │   ├── GraphPanel.jsx  # Iframe wrapper for graph.html
│   │   ├── CloneModal.jsx  # New Project modal — Git clone + manual 3-file upload
│   │   ├── UploadPanel.jsx # Upload tab — drag-and-drop for graph files
│   │   ├── GuidePanel.jsx  # How-to + MCP server configuration
│   │   ├── Sidebar.jsx     # Project list + navigation
│   │   ├── AppShell.jsx    # Outer layout
│   │   ├── PromptComposer.jsx
│   │   ├── StatusPill.jsx
│   │   ├── RouteBadge.jsx
│   │   └── ParticleBackground.jsx
│   ├── hooks/
│   │   ├── useChat.js      # sendMessage, buildRichContext, SSE streaming, conversations
│   │   ├── useGraph.js     # Load graph.json + graph.db from IDB, expose FTS5 queries
│   │   ├── useProjects.js  # Clone, delete, rename projects
│   │   ├── useLLM.js       # LLM URL/token/model management
│   │   ├── useUpload.js    # File upload to IDB + server
│   │   └── useAuth.js      # OIDC authentication
│   ├── services/
│   │   ├── apiClient.js    # SSE streaming (streamSse) + REST helpers
│   │   ├── llmService.js   # LLM relay + intent classification
│   │   ├── graphifyService.js  # Graphify query/explain/code-chunks API
│   │   ├── graphService.js # CRG graph data fetching
│   │   ├── projectsService.js  # Project CRUD
│   │   └── mcpService.js   # MCP server upload
│   ├── utils/
│   │   ├── graphQueries.js # 8 FTS5 query helpers
│   │   ├── intentDetector.js   # Prompt classification
│   │   └── idb.js          # IndexedDB wrapper
│   ├── config/
│   │   └── endpoints.js
│   └── index.css           # Tailwind + dark theme + markdown styles
├── Dockerfile
├── pyproject.toml
├── requirements.txt
└── vite.config.js
```

## Key API endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/projects` | GET | List all projects |
| `/projects/clone` | POST | Clone repo or create upload project |
| `/projects/<id>/upload-data` | POST | Upload graphify/CRG/HTML file |
| `/projects/<id>/chat-context` | POST | Build server-side context for chat |
| `/projects/<id>/graph-html` | GET | Serve graph.html with dark theme |
| `/projects/<id>/graph-data` | GET | Return graphify_data JSON |
| `/projects/<id>/graphify-query` | POST | graphify semantic query |
| `/projects/<id>/graphify-explain` | POST | graphify explain |
| `/projects/<id>/code-chunks` | POST | Source code chunks by file path |
| `/projects/<id>/crg-db` | GET | Download CRG SQLite database |
| `/llm/relay` | POST | Sync LLM relay |
| `/llm/relay/stream` | POST | SSE LLM relay |
| `/llm/models` | POST | Fetch available models |
| `/download/graph-builder` | GET | Download graph-builder script |
| `/download/mcp-server` | GET | Download standalone MCP server |
| `/download/mcp-config` | GET | Download .mcp.json |

## Running locally

```bash
cd "C:/Users/user/Desktop/Intelligraph" && python backend/app.py
```
to run on `http://localhost:5050`

```bash
# Backend
pip install -r requirements.txt
python backend/app.py          # Starts on :5050

# Frontend dev
npm install
npm run dev                    # Vite dev server, proxies to :5050

# Production build
npm run build                  # Output to dist/
```

## Graph builder (standalone)

```bash
python backend/graph_builder.py /path/to/repo
```

Generates:
- `graphify-out/graph.json` — knowledge graph
- `.code-review-graph/graph.db` — SQLite dependency graph
- `graphify-out/graph.html` — vis-network visualization

Upload all three files via the New Project → Upload tab.

## Known sharp edges

- `backend/app.py` is ~1,500 lines and has been through many mass-edits — any change must be tested
- The em dash (`—`) from LLM output appears as mojibake (`â\u0080\u0094`); the frontend replaces both forms
- Clone response includes the full graphify_data payload (~1MB+) — stripping it broke the clone flow
- Graph collapse toggle stays mounted to avoid iframe reload (animated width → 0 + opacity → 0)
- IndexedDB write ordering matters: `setActivePid` must come AFTER IDB writes for upload labels to sync
