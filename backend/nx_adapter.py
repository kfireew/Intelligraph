"""
nx_adapter.py — Optional Nx workspace enrichment for Intelligraph.

Uses Nx CLI as the source of truth for workspace metadata.
Stores raw Nx output + lightweight normalized summary.
Non-Nx repos are unaffected. Never crashes clone/index.
"""

import json
import logging
import os
import subprocess

log = logging.getLogger(__name__)

NX_INDICATORS = ["nx.json", "workspace.json", "angular.json"]


def detect_nx_workspace(repo_dir: str) -> bool:
    """Check whether repo_dir is an Nx workspace.

    Looks for nx.json, workspace.json, angular.json at root,
    or root package.json with "nx" in dependencies.
    """
    if not repo_dir or not os.path.isdir(repo_dir):
        return False

    for indicator in NX_INDICATORS:
        if os.path.isfile(os.path.join(repo_dir, indicator)):
            return True

    pkg_path = os.path.join(repo_dir, "package.json")
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path, encoding="utf-8") as f:
                pkg = json.load(f)
            all_deps = {}
            all_deps.update(pkg.get("dependencies", {}))
            all_deps.update(pkg.get("devDependencies", {}))
            if "nx" in all_deps:
                return True
        except (json.JSONDecodeError, OSError):
            pass

    return False


def _nx_command(repo_dir: str) -> list:
    """Return the nx command to use, preferring local installation."""
    local_nx = os.path.join(repo_dir, "node_modules", ".bin", "nx")
    if os.path.isfile(local_nx):
        return [local_nx]
    local_nx_cmd = os.environ.get("INTELLIGRAPH_NX_COMMAND", "")
    if local_nx_cmd:
        return local_nx_cmd.split()
    # Backward compat: check NX_MCP_COMMAND (deprecated name)
    local_nx_cmd = os.environ.get("NX_MCP_COMMAND", "")
    if local_nx_cmd:
        return local_nx_cmd.split()
    return ["npx", "nx"]  # fallback — will fail gracefully in closed networks


def _run_nx(cmd: list, cwd: str, timeout: int = 120) -> tuple[int, str, str]:
    """Run an nx command safely. Returns (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        log.warning("nx command timed out after %ds: %s", timeout, " ".join(cmd))
        return -1, "", "timeout"
    except FileNotFoundError:
        log.warning("npx not found — cannot run Nx commands")
        return -2, "", "npx not found"
    except Exception as e:
        log.warning("nx command error: %s", str(e)[:200])
        return -3, "", str(e)[:200]


def extract_nx_context(repo_dir: str) -> dict:
    """Extract Nx workspace metadata from a cloned repo.

    Uses Nx CLI as the source of truth:
      - npx nx show projects --json      (project list)
      - npx nx graph --file=<tmp>         (dependency graph)

    Returns { available, raw, projects, dependencies }.
    On failure returns { "available": False, "error": "..." }.
    """
    if not detect_nx_workspace(repo_dir):
        return {"available": False, "error": "not an nx workspace"}

    raw = {}  # full raw Nx output

    # ── 1. Project list ──
    nx_cmd = _nx_command(repo_dir)
    rc_proj, out_proj, err_proj = _run_nx(
        [*nx_cmd, "show", "projects", "--json"], cwd=repo_dir, timeout=60,
    )
    if rc_proj != 0:
        return {"available": False, "error": f"nx show projects failed: {err_proj[:200]}"}
    try:
        project_names = json.loads(out_proj)
    except json.JSONDecodeError as e:
        return {"available": False, "error": f"nx show projects JSON parse failed: {e}"}
    if not isinstance(project_names, list):
        project_names = []

    raw["projects_json"] = project_names

    # ── 2. Full dependency graph ──
    tmp_dir = os.path.join(repo_dir, ".intelligraph-nx-tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    graph_path = os.path.join(tmp_dir, "nx-graph.json")
    try:
        rc_graph, _, err_graph = _run_nx(
            [*nx_cmd, "graph", f"--file={graph_path}"], cwd=repo_dir, timeout=120,
        )
        if rc_graph == 0 and os.path.isfile(graph_path):
            with open(graph_path, encoding="utf-8") as f:
                raw["project_graph"] = json.load(f)
        else:
            log.warning("nx graph failed (rc=%d): %s", rc_graph, err_graph[:200])
            raw["project_graph"] = None
    except Exception as e:
        log.warning("nx graph error: %s", str(e)[:200])
        raw["project_graph"] = None
    finally:
        for p in [graph_path]:
            try:
                if os.path.isfile(p):
                    os.unlink(p)
            except OSError:
                pass
        try:
            if os.path.isdir(tmp_dir):
                os.rmdir(tmp_dir)
        except OSError:
            pass

    # ── 3. Build normalized summary from Nx graph output ──
    projects = []
    graph_nodes = {}
    graph_edges = []

    if raw.get("project_graph") and isinstance(raw["project_graph"], dict):
        g = raw["project_graph"].get("graph", {})
        gnodes = g.get("nodes", {})
        if isinstance(gnodes, dict):
            graph_nodes = gnodes
        gedges = g.get("edges", {})
        if isinstance(gedges, dict):
            # nx graph edges format: { "source_target": { "dependencyType": target, ... } }
            for edge_key, edge_val in gedges.items():
                parts = edge_key.rsplit("_", 1)
                if len(parts) == 2:
                    src, tgt = parts
                    graph_edges.append({"source": src, "target": tgt})
                else:
                    src = edge_val.get("source") if isinstance(edge_val, dict) else ""
                    tgt = edge_val.get("target") if isinstance(edge_val, dict) else ""
                    if src and tgt:
                        graph_edges.append({"source": src, "target": tgt})
        elif isinstance(gedges, list):
            for edge in gedges:
                src = edge.get("source") or edge.get("from") or ""
                tgt = edge.get("target") or edge.get("to") or ""
                if src and tgt:
                    graph_edges.append({"source": src, "target": tgt})

    # Build dependency map from Nx-provided graph edges
    dep_map = {}
    for edge in graph_edges:
        dep_map.setdefault(edge["source"], []).append(edge["target"])

    for name in project_names:
        node_data = graph_nodes.get(name, {})
        if isinstance(node_data, dict):
            nd = node_data.get("data", {}) if isinstance(node_data, dict) else {}
        else:
            nd = {}

        root = nd.get("root") or name
        ptype = nd.get("type") or (
            "app" if "apps" in root.replace("\\", "/").split("/")
            else "lib"
        )
        tags = nd.get("tags") or []
        targets = list(nd.get("targets", {}).keys()) if isinstance(nd.get("targets"), dict) else []
        project_type = nd.get("projectType") or ptype

        deps = dep_map.get(name, [])

        projects.append({
            "name": name,
            "root": root,
            "type": project_type,
            "tags": tags,
            "targets": targets,
            "dependencies": deps,
        })

    # Deduplicate dependency list
    dependencies = []
    seen = set()
    for edge in graph_edges:
        key = f"{edge['source']}->{edge['target']}"
        if key not in seen:
            seen.add(key)
            dependencies.append(edge)

    return {
        "available": True,
        "raw": raw,
        "projects": projects,
        "dependencies": dependencies,
    }