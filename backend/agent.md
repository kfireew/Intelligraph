# Intelligraph Code Intelligence

Use graph tools to explore the codebase before reading files. These tools save 80%+ tokens vs reading source code blindly.

## Tools

- **search("symbolName")** — Find symbols by name. Returns file paths, kinds, scores. Use exact names, not sentences.
- **node("symbolName")** — Get a symbol's connections: what it calls, what calls it, what it imports. ~200 tokens.
- **path("symbolA", "symbolB")** — Trace how two symbols connect. ~150 tokens. Impossible with grep.
- **impact("symbolName")** — Blast radius of changing a symbol. Who depends on it.
- **retrieve("complex question")** — Full pipeline for multi-part questions. Heavier, ~2000 tokens.
- **local_files(["path/to/file.ts"])** — Read source code. Use last, only for files you found via search/node.

## Workflow

1. search("upsertEntity") — find the symbol
2. node("upsertEntity") — see what it connects to
3. local_files(["src/services/entity.service.ts"]) — read the code

## Good vs Bad

Good: search("upsertEntity") → node("upsertEntity") → local_files(results)
Bad:  retrieve("how do I add an entity to the map") — too vague, wastes tokens
