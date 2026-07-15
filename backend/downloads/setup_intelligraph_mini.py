#!/usr/bin/env python
"""
Intelligraph-mini setup script — installs the local-first MCP server.

Usage:
  python setup_intelligraph_mini.py [--repo-dir /path/to/project]

What it does:
  1. pip installs intelligraph-mini from GitHub (includes bundled MiniLM model)
  2. Verifies the install
  3. Prints MCP config for your AI agent (.mcp.json or opencode.json)

No Docker, no web UI, no SSO. Just graph intelligence tools for your agent.
"""

import json
import os
import subprocess
import sys

REPO_URL = "git+https://github.com/kfireew/intelligraph-mini.git"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Install Intelligraph-mini MCP server")
    parser.add_argument("--repo-dir", default=".", help="Path to your project (default: current dir)")
    args = parser.parse_args()

    repo_dir = os.path.abspath(args.repo_dir)
    print(f"[intelligraph-mini] Installing from GitHub...")
    print(f"  pip install {REPO_URL}")

    r = subprocess.run([sys.executable, "-m", "pip", "install", REPO_URL], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  FAILED: {r.stderr[:500]}")
        sys.exit(1)
    print("  OK")

    # Verify
    print("[intelligraph-mini] Verifying install...")
    r = subprocess.run([sys.executable, "-c", "from intelligraph_mini import server; print('OK')"], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  Verification failed: {r.stderr[:300]}")
        sys.exit(1)
    print("  OK")

    # Print MCP config
    print()
    print("=" * 60)
    print("MCP Configuration")
    print("=" * 60)
    print()
    print("For Claude Code (.mcp.json in project root):")
    print()
    print(json.dumps({
        "mcpServers": {
            "intelligraph-mini": {
                "command": "intelligraph-mini",
                "args": ["--repo-dir", "."]
            }
        }
    }, indent=2))
    print()
    print("For opencode (opencode.json):")
    print()
    print(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            "intelligraph-mini": {
                "type": "local",
                "command": ["intelligraph-mini", "--repo-dir", "."]
            }
        }
    }, indent=2))
    print()
    print(f"To start: intelligraph-mini --repo-dir {repo_dir}")
    print(f"First run builds indexes (~60s). Subsequent runs load in ~2s.")
    print()
    print("Tools available: search, node, path, impact, local_files")


if __name__ == "__main__":
    main()
