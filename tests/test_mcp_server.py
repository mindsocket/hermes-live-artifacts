"""Tests for the stdio MCP server's tool surface (the transport itself is the mcp SDK's)."""

import json

from live_artifacts import mcp_server
from live_artifacts.server import tools


def test_serves_every_tool_plus_skill():
    names = set(mcp_server._handlers())
    assert names == {s["name"] for s, _ in tools.TOOLS} | {"live_artifact_skill"}


def test_create_description_points_at_skill_tool_not_skill_view():
    desc = mcp_server._handlers()["live_artifact_create"][0]["description"]
    assert "skill_view" not in desc
    assert "live_artifact_skill" in desc
    # the in-Hermes description is untouched
    assert "skill_view" in next(s for s, _ in tools.TOOLS if s["name"] == "live_artifact_create")["description"]


def test_skill_tool_returns_text_and_rejects_unknown():
    handler = mcp_server._handlers()["live_artifact_skill"][1]
    assert "window.claude" in handler({"name": "capabilities"}) or "claude.use" in handler({"name": "capabilities"})
    bad = json.loads(handler({"name": "../../README"}))
    assert bad["ok"] is False


def test_create_then_read_through_handlers(tools_store):
    h = mcp_server._handlers()
    made = json.loads(h["live_artifact_create"][1]({"title": "t", "html": "<p>x</p>"}))
    assert made["ok"]
    read = json.loads(h["live_artifact_read"][1]({"id": made["id"]}))
    assert read["ok"] and read["title"] == "t"
