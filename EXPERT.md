# Intelligraph — Expert Architecture Guide

Codebase intelligence platform for closed networks. Builds code graphs from Git repos, serves them via a local MCP server to opencode/Claude Code, and provides a web chat UI with LLM-powered code Q&A.

---

## System Topology

```
┌─────────────────────────────────────────────────────────┐
│  Closed Network                                         │
│                                                         │
│  ┌──────────┐    ┌──────────────────┐    ┌───────────┐  │
│  │  GitLab  │───>│  Intelligraph    │───>│  LLM Host │  │
│  │  /GitHub │    │  Pod (Docker)    │    │  (Qwen    │  │
│  └──────────┘    │  :5050           │    │  3.6-27B) │  │
│                  │                  │    └───────────┘  │
│                  │  ┌────────────┐  │                    │
│                  │  │ CRG DB     │  │    ┌───────────┐  │
│                  │  │ graphify   │  │<──>│ Developer │  │
│                  │  │ nm_index   │  │    │ Workstation│  │
│                  │  └────────────┘  │    │ (opencode)│  │
│                  └────────┬─────────┘    └─────┬─────┘  │
│                           │                    │        │
│                           │  /projects/N/sync  │        │
│                           └───────────────────>│        │
│                              (graph.db zip)    │        │
│                                                │        │
│                               ┌────────────────┴────┐   │
│                               │ Local MCP Server    │   │
│                               │ ~/.intelligraph/    │   │
│                               │ intelligraph_mcp.py │   │
│                               │ crg_intelligence.py │   │
│                               │ MiniLM model (87MB) │   │
│                               └─────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

The pod is a Docker container running on the closed network. It clones repos, builds code graphs, and serves them. The developer workstation runs opencode with the local MCP server, which syncs graph data from the pod once at setup and then operates entirely locally. Chat (web UI) goes through the pod's LLM pipeline. MCP tools run locally on the workstation.

---

## Three Retrieval Paths

The system has three completely separate code retrieval engines that share the same graph database but query it differently:

| Path | Used by | Retrieval engine | When |
|------|---------|------------------|------|
| **MCP tools** | opencode/Claude Code | CRG graph BFS + FTS5 + semantic embeddings | Developer using AI coding assistant |
| **Chat (lightweight)** | Web UI | graphify traversal + hybrid_search + snippets + CRG direct fallback | User asks a question in the web chat |
| **Chat (heavy)** | Web UI fallback | retrieval.py (planner → resolver → traversal → retriever → ranker) | Lightweight pipeline can't find the answer |

**Why three paths?** The MCP path was built for token efficiency — each tool call returns structured results (file paths, line ranges, confidence) instead of raw code. The chat path was built for natural language Q&A — it retrieves context and sends it to an LLM with an intent-specific system prompt. The heavy pipeline is the original retrieval engine, used as a fallback when the lightweight chat path can't find the symbol.

**Key difference:** MCP's `impact()` does BFS over filtered edges with breaks/safe tagging, depth=3, and lexical fallback. Chat doesn't call `impact()` directly in the lightweight path — but the heavy pipeline does call `provider.impact()` for impact/debug/refactor intents.

---

## Data Pipeline — Stage by Stage

The build pipeline runs on the pod when a project is cloned or pulled. Each stage transforms data and feeds the next:

```
clone repo → graphify update → CRG build → post-process TS symbols
→ generate node_snippets → build nm_index → generate graph.html → cleanup
```

### Stage 1: Clone (`app.py: clone_project`)

**What happens:** Git clones the repo into a temp directory under `data/repos/`. SSL verification is off in closed-network mode. A `.code-review-graphignore` file is written to exclude build artifacts (`bundle/`, `dist/`, `__generated__/`, etc.) from CRG indexing.

**Input:** Git URL (from user or Bitbucket/GitHub auth via `bb_auth.py`)
**Output:** `data/repos/<hash>/` directory with the full repo
**File:** `app.py` lines ~1103-1380

### Stage 2: Graphify (`app.py: _build_graphs`)

**What happens:** Runs `graphify update .` as a subprocess in the repo directory. Graphify is a separate tool that parses source files and builds a JSON graph with nodes (symbols), links (relationships), and communities (clusters of related code).

**Input:** Repo directory on disk
**Output:** `graphify-out/graph.json` — a JSON file with:
- `nodes`: array of `{id, label, source_file, community, file_type, qualified_name}` — graphify indexes functions, classes, and some variables
- `links`: array of `{source, target, type, confidence}` — edges like `calls`, `imports`, `rationale_for`
- Communities are detected via file-based clustering (igraph not available in Docker)

**What graphify indexes:** Functions, classes, some variables, and rationale notes (documentation nodes). It does NOT index type aliases or const object literals — that's why we need step 4.

**File:** `app.py` lines ~1378-1396, subprocess call to `graphify update`

### Stage 3: CRG Build (`app.py: _build_graphs`)

**What happens:** Runs `code-review-graph build` as a subprocess. CRG (code_review_graph) is a Python package that uses tree-sitter to parse ASTs and store nodes/edges in a SQLite database.

**Input:** Repo directory on disk
**Output:** `.code-review-graph/graph.db` — SQLite database with:

**nodes table:**
```sql
id INTEGER PRIMARY KEY,
kind TEXT NOT NULL,           -- File, Class, Function, Test
name TEXT NOT NULL,
qualified_name TEXT NOT NULL UNIQUE,
file_path TEXT NOT NULL,
line_start INTEGER,
line_end INTEGER,
language TEXT,
parent_name TEXT,
params TEXT,
return_type TEXT,
modifiers TEXT,
is_test INTEGER DEFAULT 0,
file_hash TEXT,
extra TEXT DEFAULT '{}',
updated_at REAL NOT NULL
```

**edges table:**
```sql
source_qualified TEXT,
target_qualified TEXT,
kind TEXT,                     -- CALLS, IMPORTS_FROM, REFERENCES, INHERITS, CONTAINS, TESTED_BY
file_path TEXT,
line INTEGER,
extra TEXT,
confidence REAL,
confidence_tier TEXT,
updated_at REAL
```

**nodes_fts (FTS5 virtual table):**
```sql
CREATE VIRTUAL TABLE nodes_fts USING fts5(
    name, signature, file_path,
    content='nodes', content_rowid='id'
)
```

**What CRG indexes for TypeScript:** Only `File`, `Class`, `Function`, `Test` nodes. The tree-sitter TypeScript parser extracts `function_declaration`, `method_definition`, `arrow_function` (→ Function) and `class_declaration`, `class` (→ Class). It does NOT extract `type_alias_declaration`, `interface_declaration`, `enum_declaration`, or `const` object literals.

**What CRG indexes as edges:** 
- `CALLS` — function A calls function B
- `IMPORTS_FROM` — file A imports from module B
- `REFERENCES` — value-position identifier references (object literal values, shorthand props, callback args). **Type annotations in function signatures do NOT create REFERENCES edges.**
- `INHERITS` — class A extends class B
- `CONTAINS` — parent contains child (class → method)
- `TESTED_BY` — test references tested code

**File:** `app.py` lines ~1397-1457, subprocess call to `code-review-graph build`

### Stage 4: Post-process TS Symbols (`app.py: _post_process_ts_symbols`)

**Why this exists:** CRG misses TypeScript type aliases (`type VehicleType = typeof VehicleTypes[number]`), const object literals (`const typeIconMap: Record<VehicleType, string> = {...}`), interfaces, and enums. Without these, `impact("VehicleTypes")` can't find `icon.ts` because the dependency chain goes through `VehicleType` (a type alias) which CRG didn't index.

**What happens:** After CRG build, the post-processing function walks all `.ts`/`.tsx` files (excluding `node_modules`, `.d.ts`, build dirs) and applies regex patterns to find symbols CRG missed.

**Phase 1 — Collect symbols:** For each file, reads content and applies these regex patterns:

```
TYPE_ALIAS_RE:    ^[ \t]*(?:export\s+)?type\s+(\w+)\s*=\s*(.+?)(?:;|\n|$)
INTERFACE_RE:     ^[ \t]*(?:export\s+)?interface\s+(\w+)
ENUM_RE:          ^[ \t]*(?:export\s+)?(?:const\s+)?enum\s+(\w+)
CONST_RECORD_RE:  ^[ \t]*(?:export\s+)?const\s+(\w+)\s*:\s*Record\s*<\s*(\w+)
CONST_TYPED_OBJ:  ^[ \t]*(?:export\s+)?const\s+(\w+)\s*:\s*([A-Z]\w+)\s*=\s*\{
CONST_ARRAY_RE:   ^[ \t]*(?:export\s+)?const\s+(\w+)\s*=\s*\[
```

For each match, it records: `(name, kind, file_path, line_start, line_end)`. Line numbers are computed via `bisect.bisect_right` on a pre-computed line-start offset array. Declaration end is found by `_find_end()` which tracks brace/bracket depth.

For type aliases, the RHS (right-hand side of `=`) is extracted from the **full declaration body** (line_start to line_end), not just the regex match group. This handles multi-line type aliases like:
```typescript
type VehicleType =
  typeof VehicleTypes[number];
```

Type references are extracted from the RHS using `\b([A-Z]\w+)\b` and filtered against `_BUILTIN_TYPES` (Array, Record, Partial, Pick, Omit, Readonly, Promise, Map, Set, etc.).

**Phase 2 — Insert nodes:** Checks if a node with the same name already exists in the same file (`SELECT 1 FROM nodes WHERE LOWER(name) = LOWER(?) AND file_path = ?`). If not, inserts with `qualified_name = "{rel_path}::{name}"`. Only rebuilds FTS index if new nodes were inserted.

**Phase 3 — Signature scanning:** Scans CRG-built function/class nodes' `signature` and `return_type` columns for post-processed type names. Creates REFERENCES edges from functions to types they mention in their signatures. This connects the two subgraphs (CRG-built functions ↔ post-processed types).

**Phase 4 — Create REFERENCES edges:** For each type reference found in Phase 1, looks up the target by name in the nodes table and creates a REFERENCES edge. For example:
- `type VehicleType = typeof VehicleTypes[number]` → edge `VehicleType → VehicleTypes`
- `const typeIconMap: Record<VehicleType, string>` → edge `typeIconMap → VehicleType`

**File:** `app.py` lines ~1372-1598

### Stage 5: Generate Snippets (`app.py: _build_graphs`)

**What happens:** After post-processing inserts new nodes, the snippet generation reads source files and extracts verbatim source code for each node.

**Process:**
1. Query all nodes with `line_start IS NOT NULL AND file_path IS NOT NULL AND name IS NOT NULL`
2. Group by `file_path` — read each source file once
3. For each node, slice lines from `line_start-1` to `line_end` (0-indexed), join, truncate to 500 chars
4. Store in `node_snippets` table

**node_snippets table (v2 only — v1 was deleted):**
```sql
qualified_name TEXT PRIMARY KEY,
node_name TEXT,
file_path TEXT,
line_start INTEGER,
line_end INTEGER,
snippet TEXT
```

This table is **dropped and recreated** on every build. Snippets power: `get_snippets()`, search lexical fallback, impact breaks/safe tagging, and the chat lightweight pipeline's source code context.

**File:** `app.py` lines ~1690-1740

### Stage 6: nm_index (`app.py: _build_nm_index`)

**What happens:** Walks `node_modules/` for `.d.ts` files and indexes their exported symbols. This enables the `package()` MCP tool and the nm_index fallback in search/impact.

**nm_symbols table:**
```sql
name TEXT, kind TEXT, file_path TEXT, line_start INTEGER,
line_end INTEGER, signature TEXT, package_name TEXT
```

**What it indexes:** Interfaces, classes, types, functions, const enums, and variable declarations in `.d.ts` files. For example, `@romach/enums` package's `.d.ts` file would have `BaseVehicleTypes`, `BaseVehicleType`, `Models`, `Model`, etc.

**File:** `app.py` lines ~1300-1369

### Stage 7: Sync to Local (`app.py: project_sync`)

**What happens:** Zips `graph.db` + `graph.json` (graphify data) + `metadata.json` (includes `original_repo_dir`, `nm_index_path`, node/edge counts) into a zip file. The local MCP server downloads this via `/projects/<pid>/sync` with an MCP token.

**Atomic update pattern:** The `mcp-update` command on the workstation:
1. Downloads zip to temp dir
2. Extracts to `_tmp_update/`
3. Moves files one at a time to the cache dir (atomic per-file)
4. Touches mtime so the running MCP detects the change

**MCP reload detection:** `_get_or_reload_provider()` checks `graph.db` mtime on every tool call. If changed, invalidates provider + embedding cache, re-initializes from new DB.

**File:** `app.py` lines ~944-996 (sync endpoint), `intelligraph-setup.ps1` (mcp-update script)

---

## Graph Schema — What's in the Database

The CRG SQLite database (`graph.db`) is the central data structure. Multiple components read from it:

### nodes table
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
kind TEXT NOT NULL,           -- File, Class, Function, Test (CRG) | Type, Variable (post-processed)
name TEXT NOT NULL,
qualified_name TEXT NOT NULL UNIQUE,
file_path TEXT NOT NULL,
line_start INTEGER,
line_end INTEGER,
language TEXT,
parent_name TEXT,
params TEXT,
return_type TEXT,
modifiers TEXT,
is_test INTEGER DEFAULT 0,
file_hash TEXT,
extra TEXT DEFAULT '{}',
updated_at REAL NOT NULL
```

**Path inconsistency:** CRG-built nodes have absolute paths (`C:\Users\...\src\file.ts`), post-processed nodes have repo-relative paths (`src/file.ts`). The `_normalize_path()` function strips the repo prefix from both, producing relative paths for internal use. The `_rewrite_path()` function joins relative paths with the local `REPO_DIR` for display.

### edges table
```sql
source_qualified TEXT,
target_qualified TEXT,
kind TEXT,
file_path TEXT,
line INTEGER,
extra TEXT,
confidence REAL,
confidence_tier TEXT,
updated_at REAL
```

**No unique constraint** — edge deduplication is done via `SELECT 1 FROM edges WHERE ... LIMIT 1` before INSERT.

### node_snippets table
```sql
qualified_name TEXT PRIMARY KEY,
node_name TEXT,
file_path TEXT,
line_start INTEGER,
line_end INTEGER,
snippet TEXT
```

Dropped and recreated on every build. Only one schema version (v2). Contains snippets for ALL nodes — CRG-built and post-processed.

### nodes_fts (FTS5 virtual table)
```sql
CREATE VIRTUAL TABLE nodes_fts USING fts5(
    name, signature, file_path,
    content='nodes', content_rowid='id'
)
```

External content FTS5 table — auto-populated from the `nodes` table. Rebuilt via `INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')` after post-processing inserts new nodes.

---

## Pod Server (`app.py`)

Flask application serving on port 5050. The main file is ~4290 lines.

### Network Modes
- `closed` (default): SSL verify off, git SSL off, allowed LLM host = `models.ai-services.idf.cts`
- `open`: SSL verify on, git SSL on, allowed LLM host = `openrouter.ai`

Controlled by `INTELLIGRAPH_NETWORK_MODE` env var. Individual settings can be overridden via `LLM_SSL_VERIFY`, `INTELLIGRAPH_GIT_SSL_VERIFY`, `LLM_ALLOWED_HOSTS`.

### SSO
- PKCE flow (no client secret needed)
- `INTELLIGRAPH_REQUIRE_SSO=true` by default in Docker
- `SECRET_KEY` must be 32+ chars when SSO is enforced (validated at startup)
- Auth handled by `_sso_guard` decorator on mutating endpoints
- `/projects/<pid>/sync` is in `_SSO_OPEN_PREFIXES` — auth via MCP token (`X-MCP-Token` header), not SSO session

### Key Endpoints

| Route | Purpose |
|-------|---------|
| `/api/v1/projects` | List/create projects |
| `/api/v1/projects/<pid>/completions` | Chat (NDJSON streaming) |
| `/api/v1/projects/<pid>/sync` | Download graph.db zip for local MCP |
| `/api/v1/projects/<pid>/mcp-update` | Trigger graph rebuild |
| `/download/agent` | Download intelligraph-agent.md |
| `/download/scout-agent` | Download intelligraph-scout.md subagent |
| `/download/enforce-plugin` | Download no-op enforcement JS |
| `/download/claude-hooks` | Download empty Claude Code hooks |
| `/download/intelligraph-mcp` | Download MCP server zip (Python + model) |
| `/download/setup-ps1` | Download setup script |
| `/graph/retrieve` | Graph API context retrieval |
| `/graph/crg/<pid>` | Serve CRG DB for online MCP |
| `/status` | Health check |

### Build Queue
`BuildQueue` class manages async clone/build operations. Projects go through states: `cloning` → `building` → `ready` / `error`. Builds run in background threads.

### Project Lifecycle
1. **Clone** (with Bitbucket/GitHub auth via `bb_auth.py`) — git clone into temp dir
2. **Build graphs** (`_build_graphs`: graphify + CRG + post-process + snippets + nm_index)
3. **Ready** — serve graph data, sync to local MCP, chat completions
4. **Pull** — re-clone latest, rebuild, atomic swap of graph.db
5. **Branch** — list git branches

---

## Local MCP Server (`intelligraph_mcp.py`)

Runs locally on the developer's workstation. Communicates with opencode via stdio (JSON-RPC). No HTTP requests to the pod during normal operation — only `/mcp-update` re-syncs graph data.

### Startup Sequence

1. Parse args: `--pod-url`, `--project-id`, `--repo-dir`, `--mcp-token`
2. Load graph metadata from pod (`/projects/<pid>/sync`)
3. Initialize `CRGProvider` with read-only SQLite connection to cached `graph.db`
4. Pre-warm: provider connection + encoder (MiniLM model) + embedding index
5. Register MCP tools (search, impact, node, path, package, search_in_file, local_files)
6. Serve via stdio JSON-RPC loop

### Tool Dispatch (`_dispatch`)

When opencode calls an MCP tool, the `_dispatch` function routes to the appropriate handler:

| Tool | Handler | What it does |
|------|---------|-------------|
| `search` | `_format_search()` | Calls `provider.search()` or `provider.hybrid_search()`, formats results with line ranges + confidence |
| `impact` | `provider.impact()` | BFS over filtered edges, breaks/safe tagging, lexical fallback |
| `node` | `provider.fast_connections()` or `provider.traverse()` | depth=1: SQL query (<100ms). depth>1: BFS adjacency build |
| `path` | `provider.path()` | Trace path between two symbols via BFS |
| `package` | `_resolve_package()` | Resolve npm package to .d.ts entry points using nm_index |
| `search_in_file` | `_search_in_file()` | Grep within a local file, returns matching lines + line numbers |
| `local_files` | `_read_local_file()` | Read full file (expensive, discouraged) |

### Key Mechanisms

**Path rewriting (`_rewrite_path`):** Every tool output passes through `_rewrite_path()` which:
1. Strips the Docker repo prefix (from `original_repo_dir` in metadata)
2. Joins the remaining repo-relative path with the local `REPO_DIR`
3. Returns forward-slashed full local path

This ensures the LLM sees paths it can use with the Read tool, regardless of whether the graph DB stores Docker-absolute or repo-relative paths.

**Path normalization (`_normalize_path` in CRGProvider):** Strips the repo prefix from DB paths. Tries `repo_dir` first, falls back to `original_repo_dir` (Docker path saved before repo deletion). Returns repo-relative paths used as keys in `file_data` dicts.

**Search caching:** `_SESSION_SEARCHES` caches results by `query|near` key. Cached responses include full result lines + positive alternatives ("Call node() or impact() for deeper info"). Auto-enriches with `fast_connections()` when the top result is a discrete entity (Function/Class/Enum/Const) with <10 connections.

**Session tracking:** `_SESSION_SEEN` tracks files seen across tool calls. Used for seen-file fallback in search — if the query matches a symbol in a previously-seen file, that file is returned.

**Source file fallback (`_scan_source_files`):** When graph + node_snippets + seen files all miss a symbol (e.g., a block-scoped const inside a function), scans source files in REPO_DIR for the query term. Returns `[L]` confidence results with `[lexical]` tag. 3-second timeout.

**MCP update detection (`_get_or_reload_provider`):** Checks `graph.db` mtime on every tool call. If changed (after `mcp-update`), invalidates provider + embedding cache, re-initializes from new DB.

---

## CRG Intelligence (`crg_intelligence.py`)

The `CRGProvider` class is the core intelligence engine. It opens a read-only SQLite connection to `graph.db` and provides 4 query modes.

### search(query, near="")

**Purpose:** Find symbols, files, or concepts by name. The primary discovery tool.

**Retrieval stages:**

1. **Pass 0a — Path LIKE:** If the query looks like a file path (contains `/` or `\` or `.ts`), queries `SELECT ... FROM nodes WHERE file_path LIKE ?`. Returns bare file paths + graph connections (up to 5 connected symbols per file via `_get_file_connections()`).

2. **Pass 0b — FTS5:** Queries the `nodes_fts` virtual table: `SELECT ... FROM nodes_fts WHERE nodes_fts MATCH ?`. Matches on node name, signature, and file_path. Returns exact matches and fuzzy matches.

3. **Pass 1b — Lexical:** Scans `node_snippets` for the query term: `SELECT ... FROM node_snippets WHERE LOWER(snippet) LIKE ?`. Catches string values, object property values, and dotted accessors that are in source code but not graph nodes. Only runs when there's no exact match or when results are sparse.

4. **nm_index fallback:** If no results from CRG, queries `nm_index.db` for the symbol name. Returns `.d.ts` file paths for external package symbols.

5. **Auto-anchor (`_auto_select_anchor`):** If `near=` is not provided, evaluates 3-8 candidate anchors from the result set. For each candidate, runs `_bfs_files_for_symbol()` to find connected files. Scores with Goldilocks scoring — peak at 25% retention (75% reduction). Too narrow (<10%) or too broad (>70%) are penalized. Uses SQL-based `_sql_near_files()` for large graphs (>5000 nodes).

6. **near= resolution:** If `near=` is provided, calls `_bfs_files_for_symbol()` which:
   - Tries exact node match in CRG
   - Falls back to snippet scan (scans `node_snippets` for the symbol string)
   - Falls back to nm_index (external package symbols)
   - Returns a set of file paths connected to the anchor within 3 graph hops

**Confidence scoring (`_compute_confidence_v2`):**
- HIGH: hybrid (FTS + semantic) + exact match + strong semantic score (≥0.5)
- MEDIUM: hybrid without exact, OR lexical-only, OR FTS-only exact, OR semantic-only strong
- LOW: semantic-only weak, or FTS-only fuzzy

**Returns:** `name (kind) file:start-end [H/M/L]` per result, plus optional `[anchor]` / `[external]` tags, plus a transparency block showing the retrieval strategy.

### hybrid_search(query, embedding_weight)

**Purpose:** Combine FTS (deterministic text matching) with semantic (embedding similarity) for best results.

**Stage 1 (deterministic):** Runs `provider.search()` — FTS5 + lexical + path matching. If results are sufficient (≥1 exact match, or ≥3 results, or path match, or has lexical hits), skip Stage 2.

**Stage 2 (semantic):** Computes embedding similarity between the query and all node names using the MiniLM model. Only runs if Stage 1 was insufficient. The `embedding_weight` parameter (default 0.4, auto-tuned) controls the blend between FTS and semantic scores.

**Stage trace:** Each result is annotated with `_stages_tried` (which stages ran) and `_stages_hit` (which stages found this result). `found_via` shows the retrieval path: "exact symbol", "lexical (term in name)", "FTS", "semantic 0.72".

### impact(target, change="add-value")

**Purpose:** Find all files affected by changing a symbol. The blast-radius analyzer.

**Step 1 — Find target nodes:** Queries `nodes` table by exact name match, then FTS fallback, then word-split FTS. If not found, returns empty list.

**Step 2 — Dynamic depth:** 
- `add-value`: depth_max=3 (type-alias chains need 2-3 hops; REFERENCES edges are sparse so 8s timeout is sufficient)
- `rename`/`remove`: depth_max=2 if <2000 nodes, 1 if ≥2000 (CALLS edges explode)
- `full`: depth_max=0 (unlimited)

**Step 3 — BFS with edge filtering:** Traverses edges where the target is either source or target. Edge kinds are filtered per change type:
- `add-value`: REFERENCES, IMPORTS_FROM, INHERITS, CONTAINS (type-position edges — these break when adding a value)
- `rename`: CALLS, IMPORTS_FROM (callers + importers break on rename)
- `remove`: all kinds
- `full`: all kinds

Filtered-out edges are still **traversed** (for deeper hops) but don't record files. This prevents missing chains while keeping the results focused.

**Step 4 — Graphify links (full/remove only):** For `full` and `remove` modes, also traverses graphify links as a second data source. Only depth-1 for these modes (adds noise at depth 2+).

**Step 5 — Breaks/safe tagging (ALL depths):** For each file found, fetches snippets for its symbols and scans for `_BREAKS_PATTERNS`:
```python
_BREAKS_PATTERNS = (
    "Record<", "[K in ", "switch (", "switch(", "Object.keys(",
    ".map(", ".reduce(", "satisfies ", " as const", "Partial<", "Pick<",
    "Omit<", "keyof ", "enum ", "values(", "entries(",
)
```
If a pattern is found in a symbol's snippet, the file is tagged `breaks=True` with the matched pattern. Otherwise `breaks=False` (safe).

**Step 6 — Lexical fallback (always runs for add-value):** Even after BFS finds files, scans `node_snippets` for additional type-position patterns:
1. **Resolve type aliases:** Scans snippets for `type Y = typeof X` or `type Y = X` → collects alias names
2. **Scan snippets for target + aliases:** `SELECT DISTINCT node_name, file_path, snippet FROM node_snippets WHERE LOWER(snippet) LIKE ?`
3. **Python-side pattern detection:** For each matching snippet, checks which `_BREAKS_PATTERNS` appear (decoupled from name matching — catches `switch (cat as VehicleType)`, `.map((v: VehicleType) => ...)` etc.)
4. New files tagged `source="lexical"`, `breaks=True`

**Step 7 — Risk-priority sort:**
- Tier 0: depth 0 (definition file)
- Tier 1: depth 1 + breaks=True (directly affected, exhaustive pattern)
- Tier 2: depth 1 + breaks=False (imports but doesn't break)
- Tier 3: depth 1 + unscanned (no snippet available)
- Tier 4: depth 2+ (indirect, unscanned)
- Tests are pushed to bottom of any tier (+10)

### fast_connections(target)

**Purpose:** Get direct connections (callers, callees) for a symbol. Used by `node()` for depth=1 and by cached search enrichment.

**Implementation:** Two SQL queries (incoming + outgoing edges) with DISTINCT and LEFT JOIN to nodes for file paths. No adjacency build, no BFS — just direct SQL. <100ms.

**Returns:** `{nodes: [...], edges: [{source, target, type, source_file, source_line, ...}], stats: {nodes, edges, est_tokens}}`

### Post-processed TS Symbols

For a detailed explanation of how post-processing works, see **Stage 4** in the Data Pipeline section above.

**Quick reference table:**

| Pattern | Node kind | Edge created |
|---------|-----------|------|
| `type X = typeof Y[number]` | Type | X → Y (REFERENCES) |
| `type X = Y` | Type | X → Y (REFERENCES) |
| `interface X` | Type | — |
| `enum X` | Type | — |
| `const X: Record<Y, ...>` | Variable | X → Y (REFERENCES) |
| `const X: Y = {` | Variable | X → Y (REFERENCES) |
| `const X = [...]` | Variable | — |
| Function signature mentions type Y | — | function → Y (REFERENCES) |

---

## Fallback Chains — The Logic of Degradation

The system is designed around a principle: **always return something useful, never return nothing.** Each retrieval path has a cascade of fallbacks. If the primary method fails, the next method fires. Understanding the order and conditions of each fallback is critical.

### MCP search() fallback chain

When the LLM calls `search("query")`, the retrieval goes through these stages in order:

```
1. Path LIKE (if query looks like a file path)
   └─ hit? → return file paths + graph connections
   └─ miss? ↓

2. FTS5 (nodes_fts MATCH)
   └─ hit? → return name (kind) file:start-end [H/M/L]
   └─ miss? ↓

3. Lexical (node_snippets LIKE)
   └─ hit? → return with [M] confidence, reason="lexical"
   └─ miss? ↓

4. nm_index (if nm_index.db exists)
   └─ hit? → return with [external] tag, file from .d.ts
   └─ miss? ↓

5. Source file scan (_scan_source_files)
   └─ hit? → return with [L] [lexical] tag, ~3s timeout
   └─ miss? ↓

6. Seen-file fallback (previously visited files in session)
   └─ hit? → return with [M] confidence, reason="seen_file_fallback"
   └─ miss? → "No symbols found matching '{query}'"
```

**Why so many fallbacks?** The graph is built by tree-sitter + regex post-processing. It misses block-scoped consts, string literals, computed property keys, and symbols in external packages. Each fallback targets a different gap:
- Path LIKE catches file-path queries (the LLM often passes file names)
- FTS5 catches exact and fuzzy name matches
- Lexical catches values in source code that aren't graph nodes (e.g., `TRACK_ZIK` as an object property value)
- nm_index catches external package symbols (e.g., `BaseVehicleTypes` in `@romach/enums`)
- Source file scan catches anything in the source files that the graph doesn't have
- Seen-file fallback catches symbols the LLM has already seen in a previous tool call

### MCP search() near= resolution fallback chain

When `near="SymbolName"` is passed, the system needs to find the files connected to that symbol:

```
1. Exact node match (SELECT FROM nodes WHERE name = ?)
   └─ hit? → BFS 3 hops from node → return file set
   └─ miss? ↓

2. Snippet scan (node_snippets WHERE snippet LIKE '%symbol%')
   └─ hit? → return files containing the symbol in source
   └─ miss? ↓

3. nm_index lookup (nm_symbols WHERE name = ?)
   └─ hit? → return .d.ts files + CRG files that import it
   └─ miss? ↓

4. Basename path fallback (file_path LIKE '%symbol%')
   └─ hit? → return matching files (catches near="icon-resolver")
   └─ miss? → near= unresolved, search returns unfiltered results
```

**When near= is unresolved:** The search runs without the filter, but results are tagged with `[near= unresolved]` in the transparency block. The agent.md guidance tells the LLM to use a symbol from those unfiltered results as `near=` next time.

### MCP impact() fallback chain

When `impact("SymbolName", change="add-value")` is called:

```
1. Find target node (exact name → FTS → word-split FTS)
   └─ miss? → return [] ("target not found")
   └─ hit? ↓

2. BFS over filtered edges (REFERENCES, IMPORTS_FROM, INHERITS, CONTAINS)
   depth_max=3, 8s timeout
   └─ found files? → proceed to breaks/safe tagging
   └─ timeout? → return partial results with [TIMED_OUT]
   └─ no files? ↓

3. Lexical fallback (always runs for add-value):
   a. Resolve type aliases (scan snippets for "type Y = typeof X")
   b. Scan snippets for target + aliases (WHERE LOWER(snippet) LIKE ?)
   c. Python-side pattern check (which _BREAKS_PATTERNS appear?)
   └─ found files? → add with source="lexical", breaks=True
   └─ no files? → return only BFS results
```

**Why the lexical fallback always runs:** The graph has gaps. Post-processing catches most type aliases and const objects, but it's regex-based. Complex TypeScript patterns (conditional types, mapped types) are missed. The lexical fallback scans source code snippets directly, catching patterns the graph doesn't have edges for.

### Chat lightweight pipeline fallback chain

When a user asks "What is typeIconMap?" in the web chat:

```
1. Graphify exact match (graphify nodes WHERE label = target)
   └─ hit? → traverse graphify links, build context
   └─ miss? ↓

2. Graphify fuzzy match (label contains target)
   └─ hit? → traverse graphify links, build context
   └─ miss? ↓

3. hybrid_search (CRG DB: FTS + semantic + lexical)
   └─ hit? → try to match result back to graphify node
      └─ graphify match? → build graphify context
      └─ no graphify match? ↓

4. CRG direct fallback (NEW):
   └─ Use CRG result directly:
      - fast_connections() for neighbors (SQL, <100ms)
      - get_snippets() for source code
      - Build context with mode="lightweight+crg"
   └─ no CRG result? ↓

5. nm_index fallback (NEW):
   └─ Query nm_index.db for external package symbols
   └─ hit? → build context with mode="lightweight+nm"
   └─ miss? ↓

6. Heavy pipeline (retrieval.retrieve_context):
   └─ planner → resolver → traversal → ranker → retriever
   └─ Intelligence providers: provider.search() or provider.impact()
   └─ Merge graphify + CRG results
   └─ miss? → LLM gets no context, answers from training data
```

**The key insight:** Before our fix, step 4 (CRG direct) didn't exist. When `hybrid_search` found a post-processed node in CRG but it wasn't in graphify, the code threw away the result and fell to the heavy pipeline (~500ms). Now, the CRG direct fallback builds context from CRG directly (~50ms), keeping the lightweight path fast.

### MCP _format_search() caching + enrichment chain

When search results are returned, the formatting layer adds context:

```
1. Check cache (_SESSION_SEARCHES by query|near key)
   └─ cached? → return [CACHED] with previous results + alternatives
   └─ not cached? ↓

2. Run search/hybrid_search
   └─ got results? ↓

3. Stale path check (build_valid_paths)
   └─ >50% stale? → skip [stale] tagging (systemic path issue)
   └─ some stale? → tag with [stale], push to bottom of results

4. Auto-enrichment (top result is discrete entity with <10 connections)
   └─ yes? → run fast_connections() inline, add connections to output
   └─ no? (hub node or >10 connections) → skip enrichment

5. Transparency block (if any fallback or non-standard stage fired)
   └─ show: "Found via: [stages]"
   └─ show: "Strategy: [lexical | snippet fallback | near= unresolved]"
```

### Why the cascade matters

Each fallback exists because a real failure mode was observed in testing:

| Fallback | What failure it fixes |
|----------|---------------------|
| Lexical (search) | `search("TRACK_ZIK")` — string value in object literal, not a graph node |
| nm_index (search) | `search("BaseVehicleTypes")` — symbol in external npm package |
| Source file scan | `search("categoryResolver")` — private const not in graph (regex missed it) |
| Seen-file fallback | `search("validateEntity")` — already seen via previous `search("upsertEntity")` result |
| CRG direct (chat) | Chat asking about `typeIconMap` — in CRG but not graphify |
| Lexical (impact) | `impact("VehicleTypes")` — `icon.ts` uses `Record<VehicleType>` but no graph edge connects them |
| Type alias resolution (impact) | `impact("VehicleTypes")` — `VehicleType` is an alias, not directly referenced |

---

## Chat Pipeline (`/api/v1/projects/<pid>/completions`)

Stateless NDJSON streaming endpoint. Each call = fresh LLM request with fresh context. No conversation state persisted — callers pass `conversation_history` as input.

### Flow — Step by Step

**Step 1 — Intent detection** (`planner.detect_intent`):
Regex-based intent detection. Analyzes the prompt for keywords like "how does X work", "what is X", "what files are affected by", "show me all files that". Returns `{intent, target}` where intent is one of: `what_is`, `how_works`, `architecture`, `impact`, `coverage`, `nx_architecture`.

**Step 2a — Lightweight retrieval** (for `what_is`/`coverage` intents):

This is the fast path (~50ms). It tries to find the symbol and build context without invoking the heavy retrieval pipeline.

1. **Graphify match:** Searches `graphify_data.nodes` for the target by exact name match, then fuzzy label match. If found, traverses graphify links (depth 1) for neighbors.

2. **hybrid_search fallback:** If not in graphify, calls `provider.hybrid_search(target)` which queries the CRG DB (FTS + semantic + lexical). This finds post-processed nodes (type aliases, const objects) that graphify doesn't have.

3. **Graphify re-match:** Tries to match hybrid_search results back to graphify nodes by name. This is where the old code failed for post-processed nodes — they're in CRG but not in graphify.

4. **CRG direct fallback (NEW):** If hybrid_search found results but they're not in graphify, builds context directly from CRG:
   - Uses `fast_connections()` to get neighbors from CRG edges (SQL, <100ms)
   - Uses `get_snippets()` to fetch source code
   - Returns `mode: "lightweight+crg"` in context stats

5. **nm_index fallback (NEW):** If CRG also doesn't have the symbol, queries `nm_index.db` for external package symbols. Returns the .d.ts file path and signature.

6. **Context building:** If a match was found (graphify or CRG direct), builds a context string:
   - Symbol name, kind, file path, community name
   - Connections list (neighbors with edge types and file paths)
   - Source snippets (up to `snippet_chars` chars, from `get_snippets()`)
   - Rationale notes (graphify rationale_for edges)

**Step 2b — Heavy retrieval** (fallback):

If lightweight failed (no match found), falls back to `retrieval.retrieve_context()`:
1. `planner.plan_query()` — decomposes prompt into tasks
2. `resolver.resolve_nodes()` — finds nodes in graphify data
3. `traversal.plan_traversal()` — expands neighborhood via graphify links
4. `ranker.rank_neighborhood()` — scores files by relevance
5. `retriever.retrieve_chunks()` — fetches code chunks from files
6. **Intelligence providers:** For each task, calls `provider.search()` (for what_is), `provider.impact()` (for impact), `provider.flows()` (for how_works), or `provider.architecture()` (for architecture). Merges CRG results with graphify results.
7. `merger.merge_tasks()` — combines all task results into a context string

The heavy pipeline DOES call `provider.impact()` for impact/debug/refactor tasks, so impact improvements (depth=3, breaks/safe, lexical fallback) DO reach the chat — but only when the heavy pipeline runs (lightweight failed).

**Step 3 — System prompt** (`_build_system_prompt`):
Intent-specific prompt structure:
- `architecture`: Summary + Architecture (layers, hubs, communities, dependencies) + Key Files + References. No code snippets.
- `impact`: Summary + Affected Files (depth, risk, why) + Recommendations + References. No code snippets.
- `coverage`: Summary + Files Found (grouped by category) + References. No code snippets.
- `how_works`: Summary + Explanation (walk-through) + Code (focused snippet) + References.
- `what_is` (default): Summary + Explanation + Code (when relevant) + References.

**Step 4 — LLM call:**
POST to the allowed LLM host (`models.ai-services.idf.cts` in closed network). Streams response back as NDJSON:
```json
{"type": "progress", "step": "search", "message": "Searching code graph..."}
{"type": "answer", "answer": "...", "intent": "what_is", "sources": {...}, "context_savings": {...}, "path_warnings": [...]}
```

**Step 5 — Path verification:** `_verify_paths()` checks if file paths mentioned in the LLM response actually exist in the project. Invalid paths are returned as `path_warnings`.

**Step 6 — Logging:** Inserts into `query_logs` and `query_log_params` tables for telemetry.

### Chat vs MCP — What's Shared, What's Not

| Feature | Chat lightweight | Chat heavy | MCP |
|---------|-----------------|------------|-----|
| Graph DB | ✅ Same | ✅ Same | ✅ Same |
| Post-processed TS nodes | ✅ (CRG direct fallback) | ✅ (via provider.search) | ✅ (via search/impact) |
| nm_index | ✅ (NEW fallback) | ❌ | ✅ (via package()) |
| impact() BFS | ❌ | ✅ (provider.impact) | ✅ |
| impact() breaks/safe | ❌ | ✅ (via provider.impact) | ✅ |
| impact() lexical fallback | ❌ | ✅ (via provider.impact) | ✅ |
| impact() depth=3 | ❌ | ✅ (via provider.impact) | ✅ |
| Auto-anchor | ❌ | ❌ | ✅ |
| Semantic search | ✅ (hybrid_search) | ✅ (via provider.search) | ✅ (hybrid_search) |
| Intent-specific prompts | ✅ | ✅ | ❌ (agent.md instead) |
| Scout subagent | ❌ | ❌ | ✅ (opencode only) |
| Source file scan | ❌ | ❌ | ✅ (_scan_source_files) |

---

## opencode Integration

### Agent Guide (`agent.md` / `intelligraph-agent.md`)
- Downloaded to project root by setup script
- MCP tools as primary, grep as gap closer
- Decision Matrix for non-standard tool responses
- Delegation guidance: 3+ MCP calls → `@intelligraph-scout`

### Scout Subagent (`intelligraph-scout.md`)
- Mode: subagent (read-only)
- Permissions: `edit: deny`, `bash: ask`, `task: deny`
- Uses MCP tools: impact → search → node → Read → grep for gaps
- Returns compact brief (~90 tokens): file paths, line ranges, one-sentence descriptions
- Installed to `.opencode/agents/` by setup script
- 4x main-window token reduction (444 → 90 tokens on sandbox test)

### Enforcement Plugin (`intelligraph-enforce.js`)
- NO-OP — grep/glob/find/Select-String all allowed
- Auto-loaded from `.opencode/plugins/` at startup
- Replaces any old blocking version on setup

### Claude Code Hooks (`claude-hooks.json`)
- Empty hooks: `{"hooks": {}}`
- No PreToolUse blocks

### Setup Script (`intelligraph-setup.ps1`)
1. Downloads agent guide → project root
2. Downloads scout subagent → `.opencode/agents/` (opencode only)
3. Downloads enforcement plugin → `.opencode/plugins/` (opencode only) or hooks → `.claude/settings.json` (Claude)
4. Downloads MCP server zip → `~/.intelligraph/` (Python scripts + MiniLM model)
5. Syncs graph data → `~/.intelligraph/cache/<pid>/`
6. Configures opencode.json or .mcp.json with MCP server command
7. Creates `mcp-update` command (PowerShell profile alias + slash command)

### MCP Server Zip (`/download/intelligraph-mcp`)
Contains:
- `intelligraph_mcp.py` — MCP server (stdio JSON-RPC)
- `crg_intelligence.py` — CRG provider
- `semantic_planner.py` — encoder loader
- `models/all-MiniLM-L6-v2/` — embedding model (87MB)

---

## Docker Deployment

### Two-Stage Build

**Base image** (`Dockerfile.base` → `intelligraph-optimised:latest`):
- Python 3.14 + system deps + code_review_graph + graphify
- CPU-only PyTorch + sentence-transformers
- MiniLM model bundled
- ~5GB, rebuilt rarely

**App image** (`Dockerfile` → `intelligraph-final:latest`):
- `FROM intelligraph-optimised:latest`
- `COPY backend/ + dist/`
- 732.5 MB tar
- Rebuilt on code changes

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `INTELLIGRAPH_NETWORK_MODE` | `closed` | closed=internal hosts, open=public internet |
| `INTELLIGRAPH_REQUIRE_SSO` | `true` | Require SSO login (Docker), false for dev |
| `SECRET_KEY` | (must set) | Flask session key, min 32 chars when SSO on |
| `LLM_ALLOWED_HOSTS` | `models.ai-services.idf.cts` | Comma-separated allowed LLM hostnames |
| `LLM_SSL_VERIFY` | `false` (closed) | Verify SSL for LLM requests |
| `INTELLIGRAPH_GIT_SSL_VERIFY` | `false` (closed) | Verify SSL for git operations |
| `INTELLIGRAPH_LLM_URL` | — | Default LLM endpoint |
| `INTELLIGRAPH_LLM_MODEL` | `Qwen/Qwen3.6-27B-FP8` | Default model |
| `INTELLIGRAPH_LLM_TIMEOUT` | `120` | LLM request timeout (seconds) |
| `CRG_PARSE_WORKERS` | `4` | CRG build parallelism |
| `GRAPHIFY_MAX_WORKERS` | `4` | Graphify build parallelism |
| `TRANSFORMERS_OFFLINE` | `1` | Encoder offline mode |
| `HF_HUB_OFFLINE` | `1` | HuggingFace offline mode |

### Volumes
- `/app/backend/data` — repos, artifacts, SQLite metadata DB

### Health Check
- `curl -f http://localhost:5050/status` every 30s

---

## Key Files Reference

| File | Lines | Purpose |
|------|-------|---------|
| `backend/app.py` | ~4290 | Flask pod server: endpoints, SSO, build pipeline, chat, post-processing |
| `backend/intelligraph_mcp.py` | ~1345 | Local MCP server: tool dispatch, search formatting, path rewriting |
| `backend/crg_intelligence.py` | ~2909 | CRG provider: search, impact, node, auto-anchor, path normalization |
| `backend/semantic_planner.py` | ~640 | Embedding encoder loader, semantic routing |
| `backend/retrieval.py` | ~687 | Heavy retrieval pipeline (chat fallback) |
| `backend/planner.py` | ~220 | Intent detection (regex-based) |
| `backend/app.py: _post_process_ts_symbols` | ~230 | TS type/const/enum injection after CRG build |
| `backend/app.py: _build_nm_index` | ~70 | node_modules .d.ts symbol index |
| `backend/app.py: _build_graphs` | ~200 | Build pipeline orchestrator |
| `backend/app.py: _stream_completions` | ~350 | Chat NDJSON streaming + LLM call |
| `backend/app.py: _build_system_prompt` | ~120 | Intent-specific LLM system prompts |
| `backend/agent.md` | ~85 | MCP agent guide (opencode/Claude) |
| `backend/intelligraph-scout.md` | ~25 | Scout subagent definition |
| `backend/intelligraph-enforce.js` | ~10 | No-op enforcement plugin |
| `backend/intelligraph-setup.ps1` | ~400 | PowerShell setup script |
| `Dockerfile` | ~30 | App image build |
| `Dockerfile.base` | ~50 | Base image build (rarely rebuilt) |

---

## Token Economics

| Scenario | Main window | Subagent | Total |
|----------|-------------|----------|-------|
| Regular opencode (no MCP) | 40k (plan) | 76k (explore) | 116k |
| MCP in main window | 63k | — | 63k |
| MCP + scout subagent | ~23k (brief + plan + edit) | ~45k (exploration) | ~68k |
| Scout brief only | 90 tokens | 444 tokens | 534 tokens |

Subagent delegation doesn't reduce total tokens — it reduces **main window** tokens. The main window stays clean for planning and editing. No compaction, no context dilution.

---

## Known Limitations

1. **Windows file-locking bug** — `CRGProvider.close()` is `pass`, `_get_or_reload_provider()` doesn't close old connection before replacing. Deferred fix: atomic rename pattern.

2. **Chat lightweight doesn't call impact()** — The lightweight pipeline builds context from graphify traversal + CRG direct fallback (search + snippets + connections), but doesn't run impact() BFS. Impact questions fall to the heavy pipeline which does call `provider.impact()`. The CRG direct fallback (new) catches most `what_is`/`coverage` questions that previously fell to heavy.

3. **Post-processing is regex-based** — Misses complex TypeScript patterns: conditional types (`type X = Y extends Z ? A : B`), mapped types (`{ [K in Y]: V }`), template literal types.

4. **Path format inconsistency** — CRG-built nodes use absolute paths, post-processed nodes use relative paths. `_normalize_path` handles this but the DB has mixed formats.

5. **External package blind spot** — Symbols in `node_modules` that aren't `.d.ts` files aren't indexed. `package()` resolves entry points but can't trace usage within the app.

6. **Scout delegation depends on model capability** — Qwen3.6-27B may not always delegate on its own. Agent.md guidance encourages it, but manual `@intelligraph-scout` invocation may be needed.
