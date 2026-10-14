"""The page-side `mcp` capability, backed by Hermes' `ctx.call_mcp`.

Two gates, deliberately separate:
  1. The artifact's manifest (`capabilities.mcp.servers = [{server, tools: [...]}]`):
     which (server, tool) pairs this page may call. The agent that writes the page declares it.
  2. The operator's `plugins.entries.hermes-live-artifacts.mcp_allowlist` in config.yaml, enforced by Hermes inside
     `ctx.call_mcp` (default deny, per server). The agent cannot edit this, so it is the real outer gate.

Not implemented (they reject `capability_removed` in the shim): listTools, describeTool, invalidate, watchTool.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")  # tool names usable as page-side method names
CALL_TIMEOUT_S = 30.0

_STATUS = {
    "bad_request": 400,
    "not_in_manifest": 403,
    "blocked_by_policy": 403,
    "approval_required": 403,
    "server_not_connected": 502,
    "server_unavailable": 503,
    "tool_error": 502,
    "upstream_error": 502,
}


class McpError(Exception):
    def __init__(self, code: str, message: str, server: str | None = None, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.server, self.retryable = code, message, server, retryable
        self.status = _STATUS.get(code, 502)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.server:
            d["server"] = self.server
        if self.retryable:
            d["retryable"] = True
        return d


def validate_manifest(cfg: Any) -> dict:
    """Check `capabilities.mcp`. Every server needs a non-empty `tools` list (an empty list
    never means "all tools"). Raises ValueError with a message the agent can act on."""
    if not isinstance(cfg, dict) or not isinstance(cfg.get("servers"), list) or not cfg["servers"]:
        raise ValueError(
            'mcp must be {"servers": [{"server": "<name>", "tools": ["<tool>", ...]}, ...]}; '
            "omit mcp entirely for a page that calls no tools"
        )
    seen = set()
    for ent in cfg["servers"]:
        if not isinstance(ent, dict) or not isinstance(ent.get("server"), str) or not ent["server"].strip():
            raise ValueError("each mcp.servers entry needs a non-empty string `server`")
        tools = ent.get("tools")
        if (
            not isinstance(tools, list)
            or not tools
            or not all(isinstance(t, str) and TOOL_NAME_RE.match(t) for t in tools)
        ):
            raise ValueError(f"mcp server {ent['server']!r} needs a non-empty `tools` list of tool names")
        if ent["server"] in seen:
            raise ValueError(f"mcp server {ent['server']!r} is listed twice")
        seen.add(ent["server"])
    return cfg


def _classify(message: str) -> tuple[str, bool]:
    """Map Hermes' free-text MCP errors onto the artifact error codes. Heuristic: Hermes returns strings, not codes, so a
    reworded message degrades to `tool_error`, never to a wrong success."""
    m = message.lower()
    if "is not connected" in m:
        return "server_not_connected", False
    if "unreachable after" in m or "transport is down" in m or "timed out" in m:
        return "server_unavailable", True
    if "approval" in m or "did not approve" in m:
        return "approval_required", False
    if "missing required parameter" in m:
        return "bad_request", False
    return "tool_error", False


def call_tool(artifact: dict, body: Any, mcp_call: Callable[..., dict] | None, timeout: float = CALL_TIMEOUT_S) -> dict:
    """Run one declared tool call for a page. Returns the page-side call shape: {content, structuredContent?, payload}."""
    if not isinstance(body, dict) or not isinstance(body.get("server"), str) or not isinstance(body.get("tool"), str):
        raise McpError("bad_request", "server and tool must be strings")
    server, tool = body["server"], body["tool"]
    args = body.get("input")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise McpError("bad_request", "input must be an object", server)
    manifest = (artifact.get("capabilities") or {}).get("mcp") or {}
    allowed = next((e.get("tools", []) for e in manifest.get("servers", []) if e.get("server") == server), None)
    if allowed is None or tool not in allowed:
        raise McpError("not_in_manifest", f"{server}/{tool} is not declared in this artifact's mcp manifest", server)
    if mcp_call is None:
        raise McpError("upstream_error", "this process has no MCP bridge", server)
    try:
        env = mcp_call(server, tool, args, timeout=timeout)
    except PermissionError as e:  # operator allowlist (config.yaml) refused: message names the key to set
        raise McpError("blocked_by_policy", str(e), server) from e
    except Exception as e:
        raise McpError("upstream_error", f"{type(e).__name__}: {e}", server, retryable=False) from e
    if not isinstance(env, dict) or not env.get("ok"):
        msg = str((env or {}).get("error") if isinstance(env, dict) else env) or "tool reported an error"
        code, retry = _classify(msg)
        raise McpError(code, msg, server, retryable=retry)
    result, structured = env.get("result"), env.get("structuredContent")
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    if structured is not None:
        payload = structured
    elif isinstance(result, str):
        try:
            payload = json.loads(result)
        except ValueError:
            payload = result
    else:
        payload = result
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}], "payload": payload}
    if structured is not None:
        out["structuredContent"] = structured
    if env.get("truncated"):
        out["truncated"] = True
    return out
