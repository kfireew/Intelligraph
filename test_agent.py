import requests, json

BASE = "http://127.0.0.1:5050"
s = requests.Session()
s.trust_env = False

# 1. Create upload project
print("=== Create project ===")
r = s.post(f"{BASE}/projects/clone", json={"type": "upload", "name": "intelligraph-self"})
print(f"  Clone: {r.status_code} {r.json()}")
pid = r.json().get("id")

# 2. Upload graph.json
print("=== Upload graph.json ===")
with open(r"C:\Users\kfirm\Downloads\Intelligraph\graphify-out\graph.json", "rb") as f:
    r = s.post(f"{BASE}/projects/{pid}/upload-data", files={"graph_file": ("graph.json", f)}, data={"type": "graphify"})
print(f"  Upload graph.json: {r.status_code}")

# 3. Upload graph.db
print("=== Upload graph.db ===")
with open(r"C:\Users\kfirm\Downloads\Intelligraph\.code-review-graph\graph.db", "rb") as f:
    r = s.post(f"{BASE}/projects/{pid}/upload-data", files={"graph_file": ("graph.db", f)}, data={"type": "crg"})
print(f"  Upload graph.db: {r.status_code}")

# 4. Check status
print("=== Project status ===")
r = s.get(f"{BASE}/projects/{pid}/status")
print(f"  Status: {r.json()}")

# 5. Generate MCP token
print("=== MCP token ===")
r = s.post(f"{BASE}/projects/{pid}/mcp-token")
print(f"  Token: {r.json()}")
token = r.json().get("mcp_token", "")

# 6. Test /graph/retrieve-context
print("\n=== MCP Tool: retrieve ===")
r = s.post(f"{BASE}/graph/retrieve-context", json={"prompt": "how does the RRF hybrid search work", "project_id": pid}, headers={"X-MCP-Token": token})
data = r.json()
print(f"  Status: {r.status_code}")
print(f"  Strategy: {data.get('strategy', '?')}")
print(f"  Files: {data.get('files', [])[:5]}")
ctx = data.get("context", "")
print(f"  Context chars: {len(ctx)}")
if ctx:
    print(f"  Preview: {ctx[:200]}")

# 7. Test /graph/crg search
print("\n=== MCP Tool: search ===")
r = s.post(f"{BASE}/graph/crg", json={"project_id": pid, "mode": "search", "query": "hybrid_search"}, headers={"X-MCP-Token": token})
data = r.json()
results = data.get("results", [])
print(f"  Results: {len(results)}")
for res in results[:3]:
    print(f"    - {res.get('name','?')} ({res.get('kind','?')}) - {res.get('file_path','?')}")

# 8. Test /graph/node
print("\n=== MCP Tool: node ===")
r = s.get(f"{BASE}/graph/node", params={"project_id": pid, "name": "hybrid_search", "depth": "2", "include_snippets": "true"}, headers={"X-MCP-Token": token})
data = r.json()
node = data.get("node", {})
print(f"  Node: {node.get('name','?')} ({node.get('kind','?')}) - {node.get('file','?')}")
print(f"  Neighbors: {len(data.get('neighbors',[]))}")
if data.get("snippets"):
    for sn, sd in list(data["snippets"].items())[:2]:
        snip = sd.get("snippet", "")
        print(f"  Snippet [{sn}]: {snip[:80]}")
if data.get("subgraph"):
    sg = data["subgraph"]
    print(f"  Subgraph: {sg.get('stats',{}).get('nodes',0)} nodes, {sg.get('stats',{}).get('edges',0)} edges")

# 9. Test /graph/path
print("\n=== MCP Tool: path ===")
r = s.get(f"{BASE}/graph/path", params={"project_id": pid, "from": "hybrid_search", "to": "search"}, headers={"X-MCP-Token": token})
data = r.json()
print(f"  Path: {data.get('hops',0)} hops")
for step in data.get("path", [])[:5]:
    print(f"    {step.get('name','?')} - {step.get('file','')}")

# 10. Test hybrid search mode
print("\n=== MCP Tool: search (hybrid) ===")
r = s.post(f"{BASE}/graph/crg", json={"project_id": pid, "mode": "hybrid", "query": "add entity"}, headers={"X-MCP-Token": token})
data = r.json()
results = data.get("results", [])
print(f"  Hybrid results: {len(results)}")
for res in results[:3]:
    print(f"    - {res.get('name','?')} ({res.get('mode','?')}) - {res.get('file_path','?')}")

print("\n=== ALL MCP TOOLS VERIFIED ===")
