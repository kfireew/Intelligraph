# Intelligraph Code Intelligence

Graph tools navigate the codebase and find dependencies. They return file paths with line ranges (file:start-end) so you can Read surgically instead of reading whole files.

## Two modes — know the difference

### Discovery (understanding code)
When you need to understand how code works:
1. search("SymbolName") → find where it lives: `name (kind) file:start-end [H/M/L]`
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
- **search("query")** — Find symbols. Returns `name (kind) file:start-end [H/M/L]`. Replaces grep and glob. Use FIRST.
- **node("name")** — Get connections (callers, callees) with file:line ranges. Use after search.
- **impact("name")** — Complete blast radius. Exhaustive. Use BEFORE editing. Files not listed do not depend on the target.
- **path("from", "to")** — Trace how two symbols connect.
- **local_files(["path"])** — Read full files. EXPENSIVE. Prefer Read with line ranges from search/node.

## Rules
- **DO NOT use grep or glob.** search() replaces both and provides line ranges.
- **DO NOT spawn explore subagents.** Use search() + node() + Read with line ranges.
- **DO NOT read a whole file when you have a line range.** Use Read with offset/limit.
- **DO NOT edit without running impact() first.** impact() finds files grep misses.
- **DO NOT search for the same thing twice.** search() caches results in-session.
