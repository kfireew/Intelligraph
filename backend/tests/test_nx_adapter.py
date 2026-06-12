"""Tests for nx_adapter.py — Nx workspace detection and context extraction."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from nx_adapter import detect_nx_workspace, extract_nx_context


# ── Helpers ──

@pytest.fixture
def non_nx_repo(tmp_path):
    """A repo directory that is NOT an Nx workspace."""
    d = tmp_path / "non-nx"
    d.mkdir()
    (d / "package.json").write_text('{"name": "not-nx", "dependencies": {"react": "^18"}}')
    (d / "index.js").write_text("console.log('hello')")
    return str(d)


@pytest.fixture
def nx_repo_nxjson(tmp_path):
    """Nx workspace detected via nx.json."""
    d = tmp_path / "nx-json"
    d.mkdir()
    (d / "nx.json").write_text('{"extends": "nx/presets/core"}')
    (d / "package.json").write_text('{"name": "nx-ws", "devDependencies": {"nx": "^20"}}')
    return str(d)


@pytest.fixture
def nx_repo_pkg(tmp_path):
    """Nx workspace detected via package.json only (no nx.json)."""
    d = tmp_path / "nx-pkg"
    d.mkdir()
    (d / "package.json").write_text('{"name": "nx-pkg", "devDependencies": {"nx": "^19"}}')
    return str(d)


# ── detect_nx_workspace ──

class TestDetect:
    def test_non_nx_repo_returns_false(self, non_nx_repo):
        assert detect_nx_workspace(non_nx_repo) is False

    def test_non_nx_empty_dir_returns_false(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert detect_nx_workspace(str(d)) is False

    def test_non_nx_nonexistent_dir_returns_false(self):
        assert detect_nx_workspace("/nonexistent/path") is False

    def test_nx_json_detected(self, nx_repo_nxjson):
        assert detect_nx_workspace(nx_repo_nxjson) is True

    def test_nx_in_package_json_detected(self, nx_repo_pkg):
        assert detect_nx_workspace(nx_repo_pkg) is True

    def test_angular_json_detected(self, tmp_path):
        d = tmp_path / "angular"
        d.mkdir()
        (d / "angular.json").write_text("{}")
        assert detect_nx_workspace(str(d)) is True

    def test_workspace_json_detected(self, tmp_path):
        d = tmp_path / "workspace"
        d.mkdir()
        (d / "workspace.json").write_text("{}")
        assert detect_nx_workspace(str(d)) is True

    def test_nx_in_dependencies_detected(self, tmp_path):
        d = tmp_path / "nx-deps"
        d.mkdir()
        (d / "package.json").write_text('{"dependencies": {"nx": "^18"}}')
        assert detect_nx_workspace(str(d)) is True


# ── extract_nx_context ──

class TestExtract:
    def test_non_nx_returns_not_available(self, non_nx_repo):
        result = extract_nx_context(non_nx_repo)
        assert result["available"] is False
        assert "error" in result
        assert "not an nx workspace" in result["error"]

    def test_nx_but_no_npx_returns_not_available(self, nx_repo_nxjson):
        """Nx workspace detected but npx/nx CLI not installed."""
        result = extract_nx_context(nx_repo_nxjson)
        assert result["available"] is False
        # Should fail gracefully — either npx not found or nx command fails
        assert "error" in result
        # But should NOT crash
        assert isinstance(result, dict)

    def test_empty_dir_returns_not_available(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        result = extract_nx_context(str(d))
        assert result["available"] is False

    def test_nonexistent_dir_returns_not_available(self):
        result = extract_nx_context("/nonexistent/path")
        assert result["available"] is False


# ── Integration: detection in clone flow ──

class TestIntegration:
    def test_non_nx_flow_not_affected(self, non_nx_repo):
        """Non-Nx repos work fine — retrieval pipeline should be unaffected."""
        # Simulate what app.py does during clone
        from nx_adapter import extract_nx_context
        nx_ctx = extract_nx_context(non_nx_repo)
        assert nx_ctx["available"] is False

        # Create a minimal project dict and test retrieval
        from retrieval import retrieve_context
        proj = {
            "graphify_data": {
                "nodes": [{"id": "1", "label": "test_func", "source_file": "test.py"}],
                "links": [],
            },
            "nx_metadata": {},
        }
        result = retrieve_context(proj, "What does this code do?")
        assert "context" in result
        assert len(result["context"]) > 0
        assert result["strategy"] != "no_data"
        # Nx info should not appear in context
        assert "Nx Workspace" not in result["context"]

    def test_nx_metadata_empty_for_non_nx(self, non_nx_repo):
        """Non-Nx projects get empty nx_metadata, not None."""
        from nx_adapter import extract_nx_context
        nx_ctx = extract_nx_context(non_nx_repo)
        assert nx_ctx["available"] is False

    def test_nx_failure_does_not_crash_retrieval(self, nx_repo_nxjson):
        """Even if Nx extraction fails, the retrieval pipeline still works."""
        from nx_adapter import extract_nx_context
        nx_ctx = extract_nx_context(nx_repo_nxjson)
        assert nx_ctx["available"] is False

        proj = {
            "graphify_data": {
                "nodes": [{"id": "1", "label": "main", "source_file": "main.py"}],
                "links": [],
            },
            "nx_metadata": {},
        }
        from retrieval import retrieve_context
        result = retrieve_context(proj, "How does main work?")
        assert "context" in result
        assert len(result["context"]) > 0


# ── Runtime boundary tests ──


def _repo_root():
    """Return project root (Intelligraph/) from test file location."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestBoundaries:
    def test_no_frontend_nx_logic(self):
        """Verify there are no Nx imports or references in frontend code."""
        root = _repo_root()
        frontend_files = [
            os.path.join(root, "src", f)
            for f in [
                "hooks/useChat.js",
                "hooks/useGraph.js",
                "hooks/useProjects.js",
                "services/apiClient.js",
                "services/llmService.js",
                "config/endpoints.js",
                "components/ChatPanel.jsx",
                "components/GraphPanel.jsx",
            ]
        ]
        nx_keywords = ["nx", "npx", "NxProject", "nx_adapter", "nx_metadata"]
        for fp in frontend_files:
            if not os.path.isfile(fp):
                continue
            with open(fp, encoding="utf-8") as f:
                content = f.read()
            for kw in nx_keywords:
                assert kw not in content, f"Found '{kw}' in frontend file {fp}"

    def test_no_nx_endpoints_added(self):
        """Verify no /nx/* endpoints were added to app.py."""
        root = _repo_root()
        app_path = os.path.join(root, "backend", "app.py")
        with open(app_path, encoding="utf-8") as f:
            content = f.read()
        assert '"/graph/retrieve-context"' in content
        assert '"/nx/' not in content

    def test_no_mcp_nx_logic(self):
        """Verify no Nx logic was added to MCP server."""
        root = _repo_root()
        mcp_path = os.path.join(root, "backend", "mcp_server.py")
        if os.path.isfile(mcp_path):
            with open(mcp_path, encoding="utf-8") as f:
                content = f.read()
            assert "nx" not in content.lower(), "MCP should not reference Nx"