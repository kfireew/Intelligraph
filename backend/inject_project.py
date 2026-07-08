"""Inject project 2 (graphify) from existing artifacts into SQLite."""
import sqlite3, json, os

DB_PATH = "C:/Users/kfirm/Downloads/Intelligraph/backend/data/temp/intelligraph.db"
ARTIFACTS_DIR = "C:/Users/kfirm/Downloads/Intelligraph/backend/data/artifacts/2"

with open(os.path.join(ARTIFACTS_DIR, "graph.json"), "r", encoding="utf-8") as f:
    graphify_data = json.load(f)

nodes = len(graphify_data.get("nodes", []))
links = len(graphify_data.get("links", []))
print(f"Graph: {nodes} nodes, {links} edges")

safe = {
    "name": "graphify",
    "git_url": "https://github.com/Graphify-Labs/graphify.git",
    "status": "ready",
    "nodes": nodes,
    "edges": links,
    "graphify_data": graphify_data,
    "crg_db_path": os.path.join(ARTIFACTS_DIR, "graph.db"),
    "_has_graphify": True,
    "_has_crg": True,
    "_has_html": False,
}

conn = sqlite3.connect(DB_PATH)
conn.execute("DELETE FROM projects WHERE id = 2")
conn.execute("INSERT OR REPLACE INTO projects(id, user_key, data) VALUES(?, ?, ?)",
             (2, "local", json.dumps(safe)))
conn.commit()
conn.close()
print(f"Injected project 2 ({len(json.dumps(safe))} bytes)")
