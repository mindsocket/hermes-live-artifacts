"""Test fixtures and environment configuration."""

import os
import sys
from pathlib import Path

import pytest

from hermes_live_artifacts.server import tools
from hermes_live_artifacts.server.store import Store

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
def tools_store(monkeypatch, store):
    """Point the agent tools at `store`; module settings are restored after the test."""
    monkeypatch.setattr(tools, "_store", store)
    monkeypatch.setattr(tools, "_base_url", tools._base_url)
    monkeypatch.setattr(tools, "_link_secret", tools._link_secret)
    return store
