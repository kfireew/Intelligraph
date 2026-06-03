# HANDOVER — Intelligraph Session 2026-06-03

## Project state at session end

Commit: `10ac392` on main. All collapse-saga commits reverted. App running at `localhost:5050`.

## Working features

| Feature | Status |
|---------|--------|
| Clone GitHub/Bitbucket repos | Works. Uses `mkdtemp` for unique directories |
| graphify graph generation | Works. 1312 nodes for Receipt-OCR-Prod |
| code-review-graph generation | Works. `check_same_thread=False` fix applied |
| LLM chat (multi-question) | Works. 3-stage context builder: graphify BFS + CRG LIKE + content search |
| Chat conversations per project | Works. localStorage persistence. Cleared on project switch |
| Upload labels (green badges) | Works. IDB writes after clone. `setActivePid` after writes |
| Graph iframe with theme | Works. `rgba(0,0,0,0.8)` site colors. Toggle button inside graph |
| Delete project | Works. Removes from memory + SQLite |
| SSE stream charset | `utf-8` on stream response |
| File content injection | Works. Injects file content from repo_dir when results thin |
| Chat clear on delete | Works. `clearChats` + `localStorage.removeItem` |
| MD styles in chat | Works. Paragraphs, lists, headings, blockquotes styled |
| Em dash fix | SSE stream replaces `\u2014` → `--` |
| Delete cursor | `cursor-pointer` on delete button |

## Known bugs / unfixed

| Issue | Details |
|-------|---------|
| â appears in chat | Em dash from LLM response. Frontend has `replace(/\u2014/g, '--')` in 4 places (tokens, done, fallback, final) but still appears. Possibly em dash arrives as raw bytes before TextDecoder |
| Graph collapse toggle | NOT IMPLEMENTED. All attempts reverted. Do NOT try to implement without a clear plan first |
| Messages don't shrink-to-fit | `max-w-[88%]` should allow shrinking but messages still fill width. Check parent flex layout |
| `#0d1117` in fallback HTML pages | Lines ~797, ~800 in app.py — fallback HTML for missing projects still uses old color. Not visible during normal operation |

## Major fuckups this session

1. **apiClient.js em dash in TDZ** — `if (data?.text)` placed BEFORE `const data = frame.data`. Caused ReferenceError, all SSE frames silently dropped, LLM returned empty. Fixed by moving after declaration.

2. **chat-context nodes undefined** — `NameError: name 'nodes' is not defined` at Stage 3. Mass-edit corruption deleted `nodes = graphify_data.get("nodes", [])`. Fixed.

3. **build_from_json missing** — `G = graphify.build_from_json(graphify_data)` line deleted during mass-edit. `G` stayed None, BFS never ran. Fixed.

4. **_load_projects corrupted** — `rows = conn.execute(...)` line deleted, causing `NameError: rows`. Then `conn = _db_conn()` also deleted. Fixed.

5. **list_projects overwriting live projects** — `_load_projects()` called on every list, overwrote in-memory projects with stripped SQLite versions (no graphify_data). Fixed: guard `if row["id"] not in _projects()`.

6. **delete_project 500** — `conn = _db_conn(); conn.execute(...)` ran outside `if proj:` block, crashed on null. Also `check_same_thread=False` missing on file-based SQLite. Both fixed.

7. **clone endpoint duplicate directory** — `shutil.rmtree` with `ignore_errors=True` failed silently on Windows file locks. Fixed with `mkdtemp`.

8. **secret_key regenerated on restart** — Invalidated all session cookies, user lost projects. Fixed: hardcoded dev key.

9. **clone response too large** — `jsonify({id, **proj})` with 1MB+ graphify_data. Tried stripping, broke clone. Reverted to full response.

10. **Collapse saga** — 7 commits, 3 reverts. All approaches failed. Do NOT try again without explicit plan.

## Files to be careful with

- `backend/app.py` — 1450 lines. Fragile after many mass-edits. Test every change.
- `src/hooks/useChat.js` — 433 lines. `sendMessage`, `buildRichContext`, `clearChats`, conversations
- `src/hooks/useProjects.js` — `cloneProject` has IDB writes + `setActivePid` order
- `src/services/apiClient.js` — `streamSse` has em dash fix at line 72 (AFTER `const data`)
- `src/components/GraphPanel.jsx` — simple wrapper, no collapse logic
- `src/components/CloneModal.jsx` — upload mode with staggered animation

## How to run

```bash
cd "C:/Users/Kfir Ezer/Desktop/intelligraph/Kfirs-Intelligraph"
# Kill stuck processes
taskkill /F /IM python.exe 2>/dev/null
taskkill /F /IM node.exe 2>/dev/null
# Build frontend
npm run build
# Start backend
nohup python backend/app.py --port 5050 --host 0.0.0.0 > /tmp/intelligraph.log 2>&1 &
```

## Testing notes

- After backend restart, projects are gone from memory. Clone fresh.
- Hard refresh (Ctrl+Shift+R) after every frontend build.
- Delete project → verify it doesn't reappear on refresh/restart.
- Chat: send multiple questions, check context is relevant.
- Upload tab: labels should turn green after clone.
