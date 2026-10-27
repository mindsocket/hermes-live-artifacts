"""Tests for the stdio MCP server: tool surface, error mapping, and a real stdio round trip."""

import json
import os
import subprocess
import sys
import threading

import pytest

from live_artifacts import mcp_server, serving_mcp
from live_artifacts.server import tools


def test_serves_every_tool():
    names = set(mcp_server._handlers())
    assert names == {s["name"] for s, _ in tools.TOOLS}


def test_create_description_is_mcp_specific_and_hermes_untouched():
    schema = mcp_server._handlers()["live_artifact_create"][0]
    assert "skill_view" not in schema["description"]
    assert "live-artifacts://skills/capabilities" in schema["description"]
    assert "wake" not in schema["parameters"]["properties"]
    assert "Pass wake=false" not in schema["description"]
    # push guidance is conditional, so it is not misleading over MCP
    assert "push_on_notify is false" in schema["description"]
    hermes = next(s for s, _ in tools.TOOLS if s["name"] == "live_artifact_create")
    assert (
        "`live-artifacts:capabilities` skill" in hermes["description"] and "wake" in hermes["parameters"]["properties"]
    )


def test_export_has_no_path_over_mcp():
    assert "path" not in mcp_server._handlers()["live_artifact_export"][0]["parameters"]["properties"]


def test_read_skill_serves_resources_and_rejects_unknown():
    assert "window.claude" in mcp_server.read_skill("live-artifacts://skills/capabilities")
    for bad in ("live-artifacts://skills/../../README", "live-artifacts://skills/nope", "file:///etc/passwd"):
        with pytest.raises(ValueError):
            mcp_server.read_skill(bad)


def test_create_then_read(tools_store):
    h = mcp_server._handlers()
    text, err = mcp_server.call(h, "live_artifact_create", {"title": "t", "html": "<p>x</p>"})
    made = json.loads(text)
    assert not err and made["ok"] and made["push_on_notify"] is False
    text, err = mcp_server.call(h, "live_artifact_read", {"id": made["id"]})
    assert not err and json.loads(text)["title"] == "t"


def test_failures_set_is_error(tools_store):
    h = mcp_server._handlers()
    assert mcp_server.call(h, "live_artifact_read", {})[1]
    assert mcp_server.call(h, "live_artifact_read", {"id": [1]})[1]  # handler raises: mapped, not propagated
    text, err = mcp_server.call(h, "nope", {})
    assert err and "unknown tool" in text


def test_export_ignores_path(tools_store, tmp_path):
    h = mcp_server._handlers()
    target = tmp_path / "x.json"
    _, err = mcp_server.call(h, "live_artifact_export", {"path": str(target)})
    assert not err and not target.exists()


def test_serving_mcp_detects_subcommand(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hermes", "-p", "w", "live-artifacts", "mcp"])
    assert serving_mcp()
    monkeypatch.setattr(sys, "argv", ["hermes", "-p", "live-artifacts", "live-artifacts", "mcp"])
    assert serving_mcp()
    monkeypatch.setattr(sys, "argv", ["hermes", "live-artifacts", "list"])
    assert not serving_mcp()
    monkeypatch.setattr(sys, "argv", ["hermes", "chat"])
    assert not serving_mcp()


def test_stdio_round_trip(tmp_path):
    """Drive serve() over real stdin/stdout: stdout must carry only JSON-RPC."""
    pytest.importorskip("mcp_types")
    code = (
        f"import sys;sys.path.insert(0,{os.path.dirname(__file__)!r});import conftest;"
        "import asyncio;from live_artifacts import mcp_server;asyncio.run(mcp_server.serve())"
    )
    init = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    }
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "live_artifact_read", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
        {"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": "live-artifacts://skills/design"}},
    ]
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "HERMES_HOME": str(tmp_path)},
    )
    by_id: dict = {}
    watchdog = threading.Timer(30, proc.kill)  # a hung server fails the test instead of the CI job
    watchdog.start()
    try:
        for m in msgs:
            proc.stdin.write(json.dumps(m) + "\n")
        proc.stdin.flush()
        while not {3, 4, 5} <= by_id.keys():  # stdout is JSON-RPC only: every line must parse
            line = proc.stdout.readline()
            assert line, "server exited early"
            msg = json.loads(line)
            by_id[msg.get("id")] = msg
    finally:
        watchdog.cancel()
        proc.kill()
        proc.wait()
    assert len(by_id[2]["result"]["tools"]) == len(tools.TOOLS)
    assert by_id[3]["result"]["isError"] is True
    assert "live-artifacts://skills/design" in by_id[1]["result"]["instructions"]
    assert {r["uri"] for r in by_id[4]["result"]["resources"]} == {
        "live-artifacts://skills/capabilities",
        "live-artifacts://skills/design",
    }
    assert "Live artifact page design" in by_id[5]["result"]["contents"][0]["text"]


def test_wake_is_stripped_and_session_key_cleared(tools_store, monkeypatch):
    h = mcp_server._handlers()
    monkeypatch.setenv("HERMES_SESSION_KEY", "agent:main:x")
    monkeypatch.setattr(sys, "argv", ["x"])
    made = json.loads(mcp_server.call(h, "live_artifact_create", {"title": "t", "html": "<p>x</p>", "wake": False})[0])
    assert made["wake"] is True  # the argument was dropped
    mcp_server.os.environ.pop("HERMES_SESSION_KEY", None)  # what cmd_mcp does at start
    made = json.loads(mcp_server.call(h, "live_artifact_create", {"title": "t", "html": "<p>x</p>"})[0])
    assert made["push_on_notify"] is False
