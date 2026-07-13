# Intelligraph Code Intelligence — MCP Agent Guide

You have access to Intelligraph MCP tools that provide deep codebase intelligence.
**You must use these tools actively** — do not guess or hallucinate about code you haven't looked up.

---

## When to Use Each Tool

| User asks... | Use this tool | Why |
|---|---|---|
| "How does X work?" | `retrieve` | Full pipeline: graph + CRG + source code |
| "What is the architecture?" | `architecture` | Community structure, hubs, relationships |
| "What breaks if I change X?" | `impact` | Blast-radius over call graph edges |
| "Who calls X?" | `callers` | Incoming call edges |
| "What does X call?" | `callees` | Outgoing call edges |
| "Where is X defined?" | `search` | FTS symbol lookup |
| "Find tests for X" | `tests` | Test file discovery |
| "How does data flow through X?" | `flows` | Execution paths from entry points |
| "Show me the code in file X" | `local_files` | Read actual file contents from disk |

## Rules

1. **Always use tools before answering.** Never describe code you haven't looked up. If a tool exists for the question type, use it.

2. **Use `local_files` after graph tools.** Graph tools tell you WHICH files matter. `local_files` gives you the actual code. Use both — graph first, then read the files.

3. **Use `retrieve` for complex questions.** If the question spans multiple concepts ("how does the parser work and what breaks if I change it"), use `retrieve` — it decomposes the question and runs the full pipeline.

4. **Use specific tools for specific questions.** Don't use `retrieve` for everything. If the user asks "who calls build_graph?", use `callers` directly — it's faster and more precise.

5. **Be specific with symbol names.** Pass exact symbol names to `impact`, `callers`, `callees`, `flows`. Use `search` first if you're not sure of the exact name.

6. **Read multiple files at once.** `local_files` accepts an array of paths. Batch your reads — don't call it once per file.

## Workflow Example

User: "How does the clustering algorithm work and what would break if I changed it?"

Step 1: Call `retrieve` with the full question — gets context + source code
Step 2: Call `impact` with name="cluster" — gets blast-radius
Step 3: Call `local_files` with the top files from steps 1-2 — gets full source
Step 4: Answer using all three results

## Common Mistakes to Avoid

- **Don't skip tools and guess.** Even if you think you know the answer, verify with tools.
- **Don't use `retrieve` for simple lookups.** "Where is cluster defined?" → use `search`, not `retrieve`.
- **Don't call `local_files` before graph tools.** You won't know which files to read.
- **Don't pass full sentences as symbol names.** `impact(name="the extract function")` → use `impact(name="extract")`.
