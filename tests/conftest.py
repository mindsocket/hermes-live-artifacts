"""Test fixtures and environment configuration."""

import os
import sys
from pathlib import Path

# Add hermes-agent source if present so session-capture tests can import gateway.session_context
agent_src = os.environ.get("HERMES_AGENT_SRC")
if not agent_src:
    candidate = Path.home() / ".hermes" / "hermes-agent"
    if candidate.is_dir():
        agent_src = str(candidate)

if agent_src and Path(agent_src).is_dir() and agent_src not in sys.path:
    sys.path.insert(0, agent_src)
