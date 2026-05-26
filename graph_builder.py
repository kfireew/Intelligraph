"""graphify graph-builder: single EXE wrapper. Usage: graph-builder.exe <project-dir>"""
import subprocess, sys, os

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
    print("Step 1/2: graphify update ...")
    r = subprocess.run(["graphify", "update", "."], cwd=project_dir)
    if r.returncode != 0:
        print(f"  graphify failed with code {r.returncode}")
        sys.exit(r.returncode)
    print("  Done.")

    print("Step 2/2: code-review-graph build ...")
    r = subprocess.run(["code-review-graph", "build"], cwd=project_dir)
    if r.returncode != 0:
        print(f"  code-review-graph failed with code {r.returncode}")
        sys.exit(r.returncode)
    print("  Done.")

    print(f"""
Graphs built successfully!
  {project_dir}\\graphify-out\\graph.json
  {project_dir}\\.code-review-graph\\graph.db

Upload these two files in the graphify-qa web UI.
""")

if __name__ == "__main__":
    main()