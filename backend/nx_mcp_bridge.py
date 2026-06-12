"""
nx_mcp_bridge.py — Nx MCP bridge for closed-network environments.

Phase 2 Nx support: provides local Nx tooling when fully offline-safe.
Disabled by default. Never downloads from public internet.
Only supports allowlisted capabilities from workspace-local Nx installations.

Configuration (env vars):
    INTELLIGRAPH_ENABLE_NX_MCP=false   — master switch
    INTELLIGRAPH_NX_MCP_COMMAND=""     — path to nx binary (auto-detect if empty)
    INTELLIGRAPH_NX_MCP_TIMEOUT=10     — per-command timeout in seconds
    INTELLIGRAPH_ALLOW_NX_CLOUD=false  — Nx Cloud is never enabled by default
"""

import json
import logging
import os
import subprocess

log = logging.getLogger(__name__)

# ── Default configuration (closed-network safe) ──

ENABLE_NX_MCP = os.environ.get("INTELLIGRAPH_ENABLE_NX_MCP", "false").lower() == "true"
NX_MCP_COMMAND = os.environ.get("INTELLIGRAPH_NX_MCP_COMMAND", "")
NX_MCP_TIMEOUT = int(os.environ.get("INTELLIGRAPH_NX_MCP_TIMEOUT", "10"))
ALLOW_NX_CLOUD = os.environ.get("INTELLIGRAPH_ALLOW_NX_CLOUD", "false").lower() == "true"

# ── Allowlisted capabilities ──

ALLOWED_CAPABILITIES = {"status", "task_info", "generator_info", "affected", "docs_lookup_local"}


def get_nx_mcp_status(repo_dir: str) -> dict:
    """Return Nx MCP bridge status without attempting any connection."""
    if not ENABLE_NX_MCP:
        return {
            "available": False,
            "enabled": False,
            "reason": "INTELLIGRAPH_ENABLE_NX_MCP is not set to true",
        }
    detection = detect_offline_nx_mcp(repo_dir)
    return {
        "available": detection.get("available", False),
        "enabled": True,
        **detection,
    }


def detect_offline_nx_mcp(repo_dir: str) -> dict:
    """Detect whether Nx can run locally from workspace dependencies.
    
    Checks:
    1. repo_dir has nx in node_modules/.bin/nx
    2. repo_dir has nx in package.json dependencies
    3. A user-configured binary path exists (NX_MCP_COMMAND)
    
    Never downloads from public npm. Never calls Nx Cloud.
    """
    if not repo_dir or not os.path.isdir(repo_dir):
        return {"available": False, "error": "invalid repo directory"}

    # Check 1: workspace-local node_modules
    local_nx = os.path.join(repo_dir, "node_modules", ".bin", "nx")
    if os.path.isfile(local_nx):
        return {"available": True, "source": "local_node_modules", "command": local_nx}

    # Check 2: explicit user-configured path
    if NX_MCP_COMMAND and os.path.isfile(NX_MCP_COMMAND):
        return {"available": True, "source": "configured_path", "command": NX_MCP_COMMAND}

    # Check 3: nx in package.json (bin might resolve via npx but we verify locally)
    pkg_path = os.path.join(repo_dir, "package.json")
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path, encoding="utf-8") as f:
                pkg = json.load(f)
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "nx" in deps:
                # nx is declared in package.json but not installed
                return {"available": True, "source": "package_json_uninstalled", "command": "npx nx"}
        except Exception:
            pass

    return {"available": False, "error": "no local Nx installation found"}


def query_offline_nx_mcp(repo_dir: str, capability: str, payload: dict) -> dict:
    """Execute a Nx MCP query using only local workspace resources.
    
    Args:
        repo_dir: Root of the cloned repository.
        capability: One of ALLOWED_CAPABILITIES.
        payload: Capability-specific parameters.
    
    Returns:
        { "result": ..., "source": "local_nx_mcp", "error": None }
        or { "error": "..." }
    """
    if not ENABLE_NX_MCP:
        return {"error": "Nx MCP is disabled"}

    if capability not in ALLOWED_CAPABILITIES:
        return {"error": f"capability '{capability}' not allowed"}

    if not repo_dir or not os.path.isdir(repo_dir):
        return {"error": "invalid repo directory"}

    # Detect Nx binary
    detection = detect_offline_nx_mcp(repo_dir)
    if not detection.get("available"):
        return {"error": "Nx not available locally", "detection": detection}

    nx_cmd = detection["command"]
    if not nx_cmd:
        return {"error": "no Nx command resolved"}

    # ── Capability handlers ──

    try:
        if capability == "task_info":
            project = payload.get("project", "")
            target = payload.get("target", "")
            if not project or not target:
                return {"error": "task_info requires 'project' and 'target' parameters"}
            cmd = [nx_cmd, "show", "project", project, "--json"]
            rc, out, err = _run(cmd, repo_dir, timeout=NX_MCP_TIMEOUT)
            if rc != 0:
                return {"error": f"nx show project failed: {err[:200]}"}
            try:
                data = json.loads(out)
                targets = data.get("targets", {})
                task = targets.get(target, {})
                return {"result": task, "source": "local_nx_mcp", "project": project, "target": target}
            except json.JSONDecodeError:
                return {"error": "failed to parse nx output"}

        elif capability == "generator_info":
            collection = payload.get("collection", "")
            generator = payload.get("generator", "")
            cmd = [nx_cmd, "list", collection if collection else "", "--json"]
            if generator:
                cmd = [nx_cmd, "list", collection, generator, "--json"]
            rc, out, err = _run(cmd, repo_dir, timeout=NX_MCP_TIMEOUT)
            if rc != 0:
                return {"error": f"nx list failed: {err[:200]}"}
            return {"result": out[:5000], "source": "local_nx_mcp"}

        elif capability == "affected":
            target = payload.get("target", "")
            base = payload.get("base", "main")
            head = payload.get("head", "HEAD")
            cmd = [nx_cmd, "show", "projects", "--json", "--affected"]
            if base:
                cmd += ["--base", base]
            rc, out, err = _run(cmd, repo_dir, timeout=NX_MCP_TIMEOUT)
            if rc != 0:
                return {"error": f"nx affected failed: {err[:200]}"}
            return {"result": out[:10000], "source": "local_nx_mcp"}

        elif capability == "status":
            cmd = [nx_cmd, "report", "--json"]
            rc, out, err = _run(cmd, repo_dir, timeout=NX_MCP_TIMEOUT)
            if rc != 0:
                # fallback: basic version check
                cmd = [nx_cmd, "--version"]
                rc, out, err = _run(cmd, repo_dir, timeout=5)
                if rc == 0:
                    return {"result": {"version": out.strip()}, "source": "local_nx_mcp"}
                return {"error": f"nx status failed: {err[:200]}"}
            try:
                return {"result": json.loads(out), "source": "local_nx_mcp"}
            except json.JSONDecodeError:
                return {"result": {"raw": out[:2000]}, "source": "local_nx_mcp"}

        elif capability == "docs_lookup_local":
            # Docs lookup is limited to local Nx help text only
            query = payload.get("query", "")
            cmd = [nx_cmd, "--help"]
            rc, out, err = _run(cmd, repo_dir, timeout=5)
            if rc == 0:
                return {"result": out[:5000], "source": "local_nx_mcp"}
            return {"error": "nx help unavailable"}

        return {"error": f"unimplemented capability: {capability}"}

    except Exception as e:
        log.warning("nx_mcp query error: %s", str(e)[:200])
        return {"error": str(e)[:200]}


def _run(cmd: list, cwd: str, timeout: int = 10) -> tuple:
    """Run a command safely with timeout and capped output."""
    try:
        r = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, r.stdout[:50000], r.stderr[:5000]
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout}s"
    except FileNotFoundError:
        return -2, "", "command not found"
    except Exception as e:
        return -3, "", str(e)[:200]