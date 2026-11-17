"""
Pytest configuration: the project root on sys.path, and a private database for every test.
"""

import os
import sys

import pytest

# Add the project root directory to the Python path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, project_root)

from app.core.config import settings as _settings  # noqa: E402


@pytest.fixture(autouse=True)
def _temporary_database(tmp_path, monkeypatch):
    """Every test gets its own empty SQLite file; nothing touches /data."""
    path = str(tmp_path / "test.sqlite")
    monkeypatch.setattr(_settings, "DATABASE_PATH", path)
    return path
