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
1. impact("SymbolName") FIRST → get every file that depends on it
2. Then search/node/Read to understand the specific code you're changing
3. Edit

impact() is exhaustive — it traverses ALL edge types (CALLS, IMPORTS_FROM, INHERITS, REFERENCES, CONTAINS) with no depth limit. Files not listed do not depend on the target. You can skip grep entirely.

Skipping impact() means you WILL miss dependent files and break things.

## Tools
- **search("query", near="SymbolName")** — Find symbols, files, or concepts. Pass a specific symbol name ('UserStatus', 'zik'), a file path ('src/types/enums'), or ONE concept word ('authentication'). Do NOT pass multi-word descriptions — search('zik') not search('plane type enum plane types'). Returns `name (kind) file:start-end [H/M/L]` for symbol matches, bare paths + graph connections for path matches. Replaces grep and glob. Use FIRST.
  - **near="SymbolName" or "file/path.ts"** — filters ALL results to only files connected to this symbol or file (within 3 graph hops). Pass this on every search after the first. The first search (without near) tells you the subsystem. After that, near= is mandatory. The tool output will suggest a near= value — use it.
- **node("name")** — Get connections (callers, callees) with file:line ranges. Use after search.
- **impact("name")** — Complete blast radius. Exhaustive. Use BEFORE editing. Files not listed do not depend on the target.
- **path("from", "to")** — Trace how two symbols connect.
- **package("name")** — Resolve an npm package to its entry point files (main, types, exports). Use for external packages in node_modules that aren't in the codebase graph. Then Read the returned types/main file.
- **local_files(["path"])** — Read full files. EXPENSIVE. Prefer Read with line ranges from search/node.

## Rules
- **DO NOT use grep or glob.** search() replaces both and provides line ranges.
- **DO NOT spawn explore subagents.** Use search() + node() + Read with line ranges.
- **DO NOT read a whole file when you have a line range.** Use Read with offset/limit.
- **DO NOT edit without running impact() first.** impact() finds files grep misses.
- **DO NOT search for the same thing twice.** search() caches results in-session.
- **ONLY 1 SEARCH AT A TIME.** Do not fire multiple searches in parallel — each search hits the CRG DB + embedding model. Concurrent searches overload the pod and cause 504s.
- **Pass near= on EVERY search after the first.** The first search tells you the subsystem. After that, near= is mandatory. The tool output will suggest a near= value — use it.
- **Search what the user mentioned, not your abstraction of it.** If the user says "add a new plane type next to zik", search "zik" — not "plane type enum". Anchor on concrete names the user gives you. If no concrete name exists, use ONE concept word, not a multi-word description.
- **If search returns [L] (low confidence), do NOT retry with similar terms.** Use node() on a known symbol, Read a file you already found, or ask the user.
- **For external npm packages (node_modules), use package("name")** to find entry points, then Read the `.d.ts` file.
