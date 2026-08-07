# Intelligraph Code Intelligence

## Before you plan

Delegate to `@intelligraph-scout` first. The scout investigates the codebase, finds affected files, dependencies, and line ranges. Use the scout's findings to build your plan.

When NOT to delegate:
- Single search() or impact() call — do it directly
- Quick question ("where is X defined?") — search directly
- User asked for explanation, not editing — search directly

**Trust the scout's findings.** They include line ranges, code patterns, and how each was verified. Use those line ranges directly for editing. Do not re-read or re-search files the scout already investigated — that wastes tokens. Re-read only if confidence is LOW or the edit doesn't match the described pattern.

**After scout returns:** Write the plan FIRST. Then read only the specific line ranges the scout provided (using Read with offset/limit) to build edit oldStrings. Never read full files after scout — the scout already investigated them.

Graph tools navigate the codebase and find dependencies. They return file paths with line ranges (file:start-end) so you can Read surgically instead of reading whole files. Start with MCP tools — they provide structured navigation, line ranges, and dependency graphs. Use grep/glob to close gaps when MCP returns low-confidence results or misses a pattern.

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
- **search("query", near="SymbolName")** — Find symbols, files, or concepts. Pass a single symbol name, a file path, or ONE concept word. Returns `name (kind) file:start-end [H/M/L]` for symbol matches, bare paths + graph connections for path matches. Use FIRST. Falls back to source file scan for private consts not in the graph (results tagged [L] [lexical]).
  - **near="SymbolName" or "file/path.ts"** — filters results to files connected to this symbol or file (within 3 graph hops). Use near= only with an exact symbol or file returned by a previous search or node() call. The first search may omit near= to discover the subsystem; subsequent searches use near= with a symbol from a prior result.
- **node("name")** — Get connections (callers, callees) with file:line ranges. depth=1 is fast (SQL, <100ms) and returns file:line for each connection. Use Read directly on the returned line ranges. Use after search.
- **impact("name", change="add-value")** — Blast radius. Default (add-value) shows only files that BREAK: type-position users (Record<T>, switch, Object.keys, .map). ~5-30 files, ~500 tokens. Files tagged [breaks]/[safe], risk-sorted. Paginated — call impact(name, offset=N) for more. Pass change="full" for exhaustive (all depths, ~4k tokens) ONLY for repo-wide refactors. change="rename" for callers+importers. change="remove" for all dependents. Use BEFORE editing.
- **path("from", "to")** — Trace how two symbols connect. Returns a path summary. Use node() on each symbol in the path for detailed connections.
- **package("name")** — Resolve an npm package to its entry point files AND symbol line ranges. Returns symbol offsets so you can Read surgically instead of reading the entire .d.ts file. Use for external packages in node_modules that aren't in the codebase graph.
- **search_in_file("query", "path")** — Search within a local file for lines matching a query. Returns matching lines with line numbers. Use this instead of reading a large file in chunks — ~100 tokens vs ~4000 for chunked reads. Works on any local file, especially .d.ts files from package(). Use Read with the returned line numbers for full context.
- **local_files(["path"])** — Read full files. EXPENSIVE. Prefer Read with line ranges from search/node, or search_in_file for finding specific lines within a file.

## Closing gaps with grep/glob

MCP tools are primary — start with them. grep and glob fill gaps that structured graph navigation can't cover:

- **After search() returns [L] or no results** → grep for the symbol name to find usage sites the graph missed.
- **After impact() misses files you expect** → grep for `Record<TypeName` or `switch(TypeName` to find type-position patterns the graph doesn't trace.
- **Type-pattern searches** → grep for `Record<PlaneCategory`, `Partial<Config`, `extends BaseClass` — structural patterns that aren't graph nodes.
- **String literal searches** → grep for `'KART'`, `"production"`, error messages — runtime values that no graph indexes.
- **External package symbols** → grep for the import path or symbol name when `package()` doesn't cover it.
- **Import tracing** → grep for `from '@scope/pkg'` to find all importers of a specific module.

MCP gives you line ranges, confidence, and dependency edges. grep gives you raw text matches. Use MCP first, grep second.

## Rules
- **Start with search().** It provides line ranges, confidence levels, and graph connections. Reach for grep when MCP returns gaps.
- **Use search() + node() + Read with line ranges** instead of explore subagents.
- **Use Read with offset/limit when you have a line range** from search/node results.
- **Run impact() before editing** any type, enum, constant, or shared function. impact() finds files that grep misses.
- **Use a different search term when results are [CACHED].** search() caches results in-session — pivot to node()/impact() on a cached file path, or refine your query.
- **1 SEARCH AT A TIME.** Each search hits the CRG DB + embedding model. Concurrent searches cause 504s.
- **Use near= with symbols returned by previous search or node() results.** The first search may omit near= to discover the subsystem. If a near= anchor doesn't resolve, the search returns unfiltered results tagged with a hint — use a symbol from those results as near= next.
- **Search what the user mentioned, not your abstraction of it.** Anchor on concrete names the user gives you. If no concrete name exists, use ONE concept word.
- **If search returns [L] (low confidence), grep for the symbol name** to find usage sites, then use search_in_file() on matching files for line-level detail.
- **For external npm packages (node_modules), use package("name")** to find entry points and symbol line ranges, then Read the `.d.ts` file surgically.

### Tool Result Decision Matrix

When a tool returns a non-standard response, follow these positive pivot actions:

| Signal / Event | Next Action |
| :--- | :--- |
| **`[STATUS: TIMEOUT]`** from impact() or node() | Use the partial results already returned. Switch to `search_in_file()` or `Read()` on the primary files involved. |
| **`[CACHED]`** from search() | Change your search query to a different keyword, or pivot to `node()` / `impact()` on one of the cached file paths. |
| **All `[M]` results, no exact symbol match** | The symbol likely lives in an external package. Call `package("@scope/name")` to locate its `.d.ts` definition with line numbers. |
| **Looking for external package symbols** | Call `package("@scope/name")` FIRST to get `.d.ts` line ranges, then `Read` surgically. |
| **search() returns `[L]` or no results** | grep for the symbol name to find all usage sites. Use `search_in_file()` on matching files for line-level detail. |
| **impact() misses expected files** | grep for `Record<TypeName` or `switch(TypeName` to find type-position usage the graph might miss. |
