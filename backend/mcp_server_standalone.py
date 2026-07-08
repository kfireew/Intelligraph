"""
Intelligraph MCP Server — stdio transport via official MCP SDK.

Proxies code-graph retrieval to a running Intelligraph container.
Works with Claude Code (.mcp.json) and opencode (opencode.json).

Prerequisites:
  pip install mcp requests

Usage:
  python mcp_server_standalone.py --intelligraph-url http://localhost:5050 --project-id 1

Claude Code (.mcp.json, in project root):
  {
    "mcpServers": {
      "intelligraph": {
        "command": "python",
        "args": ["mcp_server_standalone.py", "--intelligraph-url", "http://localhost:5050", "--project-id", "1"],
        "cwd": "/path/to/your/project"
      }
    }
  }

opencode (opencode.json, in project root or ~/.config/opencode/opencode.json):
  {
    "$schema": "https://opencode.ai/config.json",
    "mcp": {
      "intelligraph": {
        "type": "local",
        "command": ["python", "mcp_server_standalone.py", "--intelligraph-url", "http://localhost:5050", "--project-id", "1"],
        "cwd": "/path/to/your/project"
      }
    }
  }
"""

import argparse
import json
import os
import sys

import requests
from mcp.server import Server
from mcp import types
from mcp.server.stdio import stdio_server

INTELLIGRAPH_URL = "http://localhost:5050"
PROJECT_ID = None
SSL_VERIFY = os.environ.get("LLM_SSL_VERIFY", "false").lower() == "true"

TOOLS = [
    types.Tool(
        name="search",
        description="Search the codebase graph for symbols, files, or concepts matching a query.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for (symbol name, concept, file pattern)"},
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="callers",
        description="Find callers of a symbol — who calls this function/method/class.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name to find callers for"},
            },
            "required": ["name"],
        },
    ),
    types.Tool(
        name="callees",
        description="Find callees of a symbol — what does this function/method call.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name to find callees for"},
            },
            "required": ["name"],
        },
    ),
    types.Tool(
        name="impact",
        description="Analyze the impact of changing a symbol — what would break.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name to analyze impact for"},
            },
            "required": ["name"],
        },
    ),
    types.Tool(
        name="architecture",
        description="Get an architecture overview of a component or the entire codebase.",
        inputSchema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Architecture question or component name"},
            },
            "required": ["prompt"],
        },
    ),
    types.Tool(
        name="tests",
        description="Find test files related to a symbol or component.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name to find tests for"},
            },
            "required": ["name"],
        },
    ),
]


def _retrieve(query: str) -> dict:
    """Call the Intelligraph container's retrieval endpoint."""
    url = f"{INTELLIGRAPH_URL}/graph/retrieve-context"
    r = requests.post(
        url,
        json={"prompt": query, "project_id": PROJECT_ID},
        timeout=30,
        verify=SSL_VERIFY,
    )
    r.raise_for_status()
    return r.json()


def _format_result(result: dict, tool_name: str, user_query: str) -> str:
    """Format the retrieval result as readable text for the LLM."""
    context = result.get("context", "")
    files = result.get("files", [])
    strategy = result.get("strategy", "")
    stats = result.get("context_stats", {})

    lines = []
    if context:
        lines.append(context)
    if files:
        lines.append("\n## Relevant Files")
        for f in files[:15]:
            if isinstance(f, dict):
                lines.append(f"- {f.get('path', f.get('name', 'unknown'))}")
            else:
                lines.append(f"- {f}")
    if strategy and strategy != "default":
        lines.append(f"\n*Strategy: {strategy}*")
    if stats:
        lines.append(f"*Stats: {json.dumps(stats)}*")
    if not lines:
        lines.append("No results found. The project may still be indexing or the query didn't match any symbols.")
    return "\n".join(lines)


def _dispatch_tool(name: str, arguments: dict) -> str:
    """Map a tool call to a retrieval query and return formatted text."""
    queries = {
        "search": lambda a: a["query"],
        "callers": lambda a: f"who calls {a['name']}",
        "callees": lambda a: f"what does {a['name']} call",
        "impact": lambda a: f"impact of {a['name']} what breaks",
        "architecture": lambda a: a["prompt"],
        "tests": lambda a: f"test {a['name']}",
    }
    if name not in queries:
        return f"Unknown tool: {name}"
    query = queries[name](arguments)
    result = _retrieve(query)
    return _format_result(result, name, query)


server = Server("intelligraph")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        text = _dispatch_tool(name, arguments)
    except requests.exceptions.ConnectionError:
        text = f"Cannot reach Intelligraph at {INTELLIGRAPH_URL}. Is the container running?"
    except requests.exceptions.Timeout:
        text = "Intelligraph request timed out (30s). The project may be large or still indexing."
    except Exception as e:
        text = f"Error: {str(e)[:500]}"
    return [types.TextContent(type="text", text=text)]


async def main():
    global INTELLIGRAPH_URL, PROJECT_ID
    parser = argparse.ArgumentParser(description="Intelligraph MCP Server (stdio)")
    parser.add_argument("--intelligraph-url", default="http://localhost:5050",
                        help="Intelligraph container URL (default: http://localhost:5050)")
    parser.add_argument("--project-id", type=int, required=True,
                        help="Project ID in the Intelligraph container")
    parser.add_argument("--project-name", default=None,
                        help="Project name to use (alternative to --project-id)")
    args = parser.parse_args()
    INTELLIGRAPH_URL = args.intelligraph_url.rstrip("/")
    PROJECT_ID = args.project_id
    
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
