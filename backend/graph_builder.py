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
        comms = {}
        if "communities" in data:
            for c in (data["communities"] or []):
                cid = c.get("id") or c.get("community_id")
                if cid is not None:
                    comms[cid] = c.get("label") or c.get("name") or str(cid)
        gf_export.to_html(G, comms, html_path)
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