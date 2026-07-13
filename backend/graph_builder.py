"""Intelligraph graph-builder: single EXE wrapper. Usage: graph-builder.exe <project-dir>"""
import subprocess, sys, os, json

def main():
    if len(sys.argv) < 2:
        print("Usage: graph-builder.exe <project-directory>")
        print("Example: graph-builder.exe C:\\my-angular-project")
        sys.exit(1)

    project_dir = os.path.abspath(sys.argv[1])
    if not os.path.isdir(project_dir):
        print(f"ERROR: directory not found: {project_dir}")
        sys.exit(1)

    print(f"Building graphs for: {project_dir}")
    print("Step 1/3: graphify update ...")
    r = subprocess.run([sys.executable, "-m", "graphify", "update", "."], cwd=project_dir)
    if r.returncode != 0:
        print(f"  graphify failed with code {r.returncode}")
        sys.exit(r.returncode)
    print("  Done.")

    print("Step 2/3: code-review-graph build ...")
    r = subprocess.run([sys.executable, "-m", "code_review_graph", "build"], cwd=project_dir)
    if r.returncode != 0:
        print(f"  code-review-graph failed with code {r.returncode}")
        sys.exit(r.returncode)
    print("  Done.")

    print("Step 3/3: graphify export graph.html ...")
    graph_path = os.path.join(project_dir, "graphify-out", "graph.json")
    html_path = os.path.join(project_dir, "graphify-out", "graph.html")
    try:
        import graphify
        import graphify.export as gf_export
        with open(graph_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        G = graphify.build_from_json(data)
        community_labels = {}
        if "communities" in data:
            for c in (data["communities"] or []):
                cid = c.get("id")
                if cid is None:
                    cid = c.get("community_id")
                if cid is not None:
                    community_labels[cid] = c.get("label") or c.get("name") or ""
        # Enrich from CRG db if available
        crg_db = os.path.join(project_dir, ".code-review-graph", "graph.db")
        if os.path.exists(crg_db):
            try:
                import sqlite3 as _sql
                from os.path import commonpath as _commonpath
                cconn = _sql.connect(f"file:{crg_db}?mode=ro", uri=True)
                cconn.row_factory = _sql.Row
                crg_fps = [r[0] for r in cconn.execute(
                    "SELECT DISTINCT file_path FROM nodes WHERE file_path IS NOT NULL"
                ).fetchall()]
                if crg_fps:
                    prefix = _commonpath(crg_fps)
                    crg_comms = {}
                    for r in cconn.execute(
                        "SELECT c.id, c.name, n.file_path FROM communities c "
                        "JOIN nodes n ON n.community_id = c.id WHERE n.file_path IS NOT NULL"
                    ).fetchall():
                        fp = (r["file_path"] or "").replace("\\", "/")
                        rel = fp[len(prefix):].lstrip("/").replace("\\", "/")
                        crg_comms.setdefault(r["id"], {"name": r["name"], "files": set()})["files"].add(rel)
                    cconn.close()
                    # Match graphify communities by file overlap
                    gf_comm_files = {}
                    for nid, ndata in G.nodes(data=True):
                        c = ndata.get("community")
                        sf = (ndata.get("source_file") or ndata.get("file_path") or "").replace("\\", "/")
                        if c is not None and sf:
                            gf_comm_files.setdefault(c, set()).add(sf)
                    for gf_cid, gf_files in gf_comm_files.items():
                        if community_labels.get(gf_cid):
                            continue
                        best_name = None
                        best_overlap = 0
                        for cd in crg_comms.values():
                            overlap = len(gf_files & cd["files"])
                            if overlap > best_overlap:
                                best_overlap = overlap
                                best_name = cd["name"]
                        if best_name and best_overlap > 0:
                            community_labels[gf_cid] = best_name
            except Exception as e:
                print(f"  Warning: CRG community name enrichment failed: {e}")
        comms = {}
        for nid, ndata in G.nodes(data=True):
            cid = ndata.get("community", 0)
            if cid not in comms:
                comms[cid] = []
                if cid not in community_labels or not community_labels.get(cid):
                    community_labels[cid] = f"Community {cid}"
            comms[cid].append(nid)
        gf_export.to_html(G, comms, html_path, community_labels=community_labels)
        print("  Done.")
    except Exception as e:
        print(f"  Warning: graph.html generation skipped: {e}")

    print(f"""
Graphs built successfully!
  {project_dir}\\graphify-out\\graph.json
  {project_dir}\\.code-review-graph\\graph.db
  {project_dir}\\graphify-out\\graph.html

Upload these three files in the Intelligraph web UI.
""")

if __name__ == "__main__":
    main()