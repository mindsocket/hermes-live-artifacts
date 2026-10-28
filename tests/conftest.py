"""Test fixtures and environment configuration."""

import os
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))

if "live_artifacts" not in sys.modules:
    try:
        import live_artifacts  # noqa: F401
    except ImportError:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "live_artifacts", str(_PLUGIN / "__init__.py"), submodule_search_locations=[str(_PLUGIN)]
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["live_artifacts"] = mod
            spec.loader.exec_module(mod)

import live_artifacts.server.tools as tools  # noqa: E402
from live_artifacts.server.store import Store  # noqa: E402

# Add hermes-agent source if present so session-capture tests can import gateway.session_context
agent_src = os.environ.get("HERMES_AGENT_SRC")
if not agent_src:
    candidate = Path.home() / ".hermes" / "hermes-agent"
    if candidate.is_dir():
        agent_src = str(candidate)

if agent_src and Path(agent_src).is_dir() and agent_src not in sys.path:
    sys.path.insert(0, agent_src)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


@pytest.fixture
def tools_store(monkeypatch, store, tmp_path):
    """Point the agent tools at `store` and a throwaway work dir; module settings are restored after the test."""
    monkeypatch.setattr(tools, "_store", store)
    monkeypatch.setattr(tools, "_work_dir", tmp_path / "work")
    monkeypatch.setattr(tools, "_base_url", tools._base_url)
    monkeypatch.setattr(tools, "_link_secret", tools._link_secret)
    monkeypatch.setattr(tools, "_index", tools._index)
    return store
