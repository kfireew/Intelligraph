"""Tests for graph_builder.py — CLI wrapper."""
import os
import sys
from unittest.mock import patch

import pytest

# Add bundle to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_no_args_shows_usage():
    """Running with no arguments prints usage and exits 1."""
    # subprocess broken on Python 3.14 Windows — test via import
    from graph_builder import main
    with pytest.raises(SystemExit) as exc:
        with patch.object(sys, "argv", ["graph_builder.py"]):
            main()
    assert exc.value.code == 1


def test_invalid_directory():
    """Non-existent directory prints error and exits 1."""
    from graph_builder import main
    with pytest.raises(SystemExit) as exc:
        with patch.object(sys, "argv", ["graph_builder.py", "/nonexistent/path/xyz"]):
            main()
    assert exc.value.code == 1