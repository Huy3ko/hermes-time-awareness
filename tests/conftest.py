"""Gemeinsame Fixtures. Kein Test importiert das echte Hermes."""

import importlib.util
import sqlite3
import sys
import types
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"

STUBBED = ("hermes_constants", "hermes_time", "hermes_cli", "hermes_cli.config")


def load_plugin(name="hta_under_test"):
    spec = importlib.util.spec_from_file_location(name, PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_stubs(home: Path):
    """Minimales Hermes, damit das Plugin ohne Gateway lädt."""
    const = types.ModuleType("hermes_constants")
    const.get_hermes_home = lambda: home
    sys.modules["hermes_constants"] = const

    cli = types.ModuleType("hermes_cli")
    cfg = types.ModuleType("hermes_cli.config")
    cfg.load_config = lambda: {}
    cli.config = cfg
    sys.modules["hermes_cli"] = cli
    sys.modules["hermes_cli.config"] = cfg


def make_messages_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE messages ("
        "session_id TEXT, role TEXT, content TEXT, timestamp REAL, "
        "display_kind TEXT, display_metadata TEXT)"
    )
    conn.commit()
    conn.close()


def add(path: Path, session: str, role: str, ts: float, content="x") -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
        (session, role, content, ts),
    )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def clean_stubs():
    yield
    for name in STUBBED:
        sys.modules.pop(name, None)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Frisches Plugin, temporäres Home, eigene DBs."""
    install_stubs(tmp_path)
    module = load_plugin()
    db = tmp_path / "state.db"
    make_messages_db(db)

    monkeypatch.setattr(module, "message_db", lambda: db)
    monkeypatch.setattr(module, "session_db", lambda: tmp_path / "ta.db")
    monkeypatch.setattr(module, "settings", lambda: {})
    module._history_cache.clear()
    module._activity_cache.clear()
    module.db_file = db          # Komfort für Tests
    return module
