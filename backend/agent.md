# Intelligraph Code Intelligence

Graph tools navigate the codebase and find dependencies. They return file paths with line ranges (file:start-end) so you can Read surgically instead of reading whole files.

## Two modes — know the difference

### Discovery (understanding code)
When you need to understand how code works:
1. search("SymbolName", near="KnownSymbol") → find where it lives: `name (kind) file:start-end [H/M/L]`
2. node("SymbolName") → see callers/callees with their file:line ranges
3. Read(file, offset=line_start, limit=line_end-line_start) → read only the relevant lines

### Editing (making changes safely)
Before editing a type, enum, constant, or shared function:
1. impact("SymbolName") FIRST → get files that BREAK when you change it (narrow by default, ~500 tokens)
2. If impact shows [breaks] files — those need updating. [safe] files import but won't break.
3. Then search/node/Read to understand the specific code you're changing
4. Edit

impact() defaults to change="add-value" — narrow mode showing only type-position users (Record<T>, switch, Object.keys, .map). Files are tagged [breaks] or [safe] based on source pattern scanning. Risk-priority sorted: breaks first, safe last, tests at bottom. Output is paginated by token budget — call impact(name, offset=N) for more pages.

Pass change="full" ONLY for repo-wide refactors where you need every transitive dependent (~4k tokens). The default narrow mode is what you want 95% of the time.

Skipping impact() means you WILL miss dependent files and break things.

## Tools
- **search("query", near="SymbolName")** — Find symbols, files, or concepts. Pass a specific symbol name ('UserStatus', 'zik'), a file path ('src/types/enums'), or ONE concept word ('authentication'). Do NOT pass multi-word descriptions — search('zik') not search('plane type enum plane types'). Returns `name (kind) file:start-end [H/M/L]` for symbol matches, bare paths + graph connections for path matches. Replaces grep and glob. Use FIRST.
  - **near="SymbolName" or "file/path.ts"** — filters results to files connected to this symbol or file (within 3 graph hops). Use near= only with an exact symbol or file returned by a previous search or node() call. Do not invent anchors from broad words such as `planes`, `filter`, or `table` — these are not graph symbols and will not resolve. The first search may omit near= to discover the subsystem; subsequent searches use near= with a symbol from a prior result.
- **node("name")** — Get connections (callers, callees) with file:line ranges. Use after search.
- **impact("name", change="add-value")** — Blast radius. Default (add-value) shows only files that BREAK: type-position users (Record<T>, switch, Object.keys, .map). ~5-30 files, ~500 tokens. Files tagged [breaks]/[safe], risk-sorted. Paginated — call impact(name, offset=N) for more. Pass change="full" for exhaustive (all depths, ~4k tokens) ONLY for repo-wide refactors. change="rename" for callers+importers. change="remove" for all dependents. Use BEFORE editing.
- **path("from", "to")** — Trace how two symbols connect.
- **package("name")** — Resolve an npm package to its entry point files AND symbol line ranges. Returns symbol offsets (e.g. `PlaneCategories (enum): 307-322`) so you can Read surgically: `Read(types_file, offset=307, limit=17)` instead of reading 587 lines. Use for external packages in node_modules that aren't in the codebase graph.
- **local_files(["path"])** — Read full files. EXPENSIVE. Prefer Read with line ranges from search/node.

## Rules
- **DO NOT use grep or glob.** search() replaces both and provides line ranges.
- **DO NOT spawn explore subagents.** Use search() + node() + Read with line ranges.
- **DO NOT read a whole file when you have a line range.** Use Read with offset/limit.
- **DO NOT edit without running impact() first.** impact() finds files grep misses.
- **DO NOT search for the same thing twice.** search() caches results in-session.
- **ONLY 1 SEARCH AT A TIME.** Do not fire multiple searches in parallel — each search hits the CRG DB + embedding model. Concurrent searches overload the pod and cause 504s.
- **Use near= only with symbols returned by previous search or node() results.** Do not invent anchors from broad words (planes, filter, table) — they are not graph symbols and will not resolve. The first search may omit near= to discover the subsystem. If a near= anchor doesn't resolve, the search returns unfiltered results tagged with a hint — use a symbol from those results as near= next.
- **Not using near= when you have a valid anchor wastes tokens.** Without near=, search returns up to 16 broad results (~2000 tokens). With a valid near=, you get 2-3 focused results (~300 tokens). But an invalid near= (a broad word, not a symbol) also wastes a round-trip. Anchor on concrete symbols from prior results.
- **Search what the user mentioned, not your abstraction of it.** If the user says "add a new plane type next to zik", search "zik" — not "plane type enum". Anchor on concrete names the user gives you. If no concrete name exists, use ONE concept word, not a multi-word description.
- **If search returns [L] (low confidence), do NOT retry with similar terms.** Use node() on a known symbol, Read a file you already found, or ask the user.
- **For external npm packages (node_modules), use package("name")** to find entry points, then Read the `.d.ts` file.
