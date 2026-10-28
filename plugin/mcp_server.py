"""Stdio MCP server exposing the live_artifact_* tools (``hermes live-artifacts mcp``).

Serves the same handlers and schemas as the in-Hermes tools, so Claude Code (or any MCP client) on the same machine shares the
artifact store with the gateway. See specs/claude-code-mcp.md.

stdout carries JSON-RPC only; anything else must go to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from importlib.metadata import version
from pathlib import Path

from . import _skill_frontmatter
from .server import tools

SKILLS_DIR = Path(__file__).parent / "skills"
MIN_MCP_MAJOR = 2  # the lowlevel Server(on_list_tools=...) / mcp_types API
logger = logging.getLogger(__name__)
RESOURCE_PREFIX = "live-artifacts://skills/"

# Hermes agents load skills as `live-artifacts:<name>`; MCP clients read them as resources instead.
_SKILL_SENTENCE = re.compile(r"Before writing the html, load [^.]*?\. ")
_SKILL_POINTER = (
    f"Before writing the html, read the `{RESOURCE_PREFIX}capabilities` MCP resource (the page-side db API) and, for "
    f"layout and theming, the `{RESOURCE_PREFIX}design` MCP resource. "
)


INSTRUCTIONS = (
    "Tools to create and edit interactive HTML pages (forms, review lists, dashboards) that the user opens by link. "
    f"Before writing page html, read the `{RESOURCE_PREFIX}capabilities` MCP resource (the page-side db API) and, for "
    f"layout and theming, the `{RESOURCE_PREFIX}design` MCP resource. Nothing is pushed when the user submits: ask them "
    "to tell you, then call live_artifact_read."
)


def _skill_names() -> list[str]:
    return sorted(p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md"))


def _skill_description(name: str) -> str:
    return str(_skill_frontmatter(SKILLS_DIR / name / "SKILL.md").get("description", ""))


def read_skill(uri: str) -> str:
    """Text of the skill a `live-artifacts://skills/<name>` URI names; ValueError if there is none."""
    name = uri.removeprefix(RESOURCE_PREFIX) if uri.startswith(RESOURCE_PREFIX) else None
    if name not in _skill_names():
        raise ValueError(f"unknown resource {uri!r}; available: {[RESOURCE_PREFIX + n for n in _skill_names()]}")
    return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def _for_mcp(schema: dict) -> dict:
    """Adapt a Hermes tool schema to MCP: no skill_view, no `wake` (nothing is ever pushed), no export `path`
    (custom paths rely on Hermes write policy, which an MCP client does not go through)."""
    props = dict(schema["parameters"]["properties"])
    desc = schema["description"]
    props.pop("wake", None)
    if schema["name"] == "live_artifact_create":
        desc = _SKILL_SENTENCE.sub(_SKILL_POINTER, desc).replace(" Pass wake=false to opt out.", "")
    if schema["name"] == "live_artifact_export":
        props.pop("path", None)
    return {**schema, "description": desc, "parameters": {**schema["parameters"], "properties": props}}


def _handlers() -> dict:
    out = {}
    for schema, handler in tools.TOOLS:
        schema = _for_mcp(schema)
        out[schema["name"]] = (schema, handler)
    return out


def call(handlers: dict, name: str, arguments: dict | None) -> tuple[str, bool]:
    """Run one tool; returns (text, is_error). Handlers report failure as JSON `{"ok": false}`; anything they raise
    is reported the same way instead of escaping as a protocol error."""
    if name not in handlers:
        return f"unknown tool {name}", True
    # keep enforcement in step with the schemas _for_mcp advertises
    dropped = {"live_artifact_export": {"path"}}.get(name, set()) | {"wake"}
    args = {k: v for k, v in (arguments or {}).items() if k not in dropped}
    try:
        text = handlers[name][1](args)
    except Exception as e:
        logger.warning("live-artifacts mcp: %s failed", name, exc_info=True)
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}), True
    try:
        failed = json.loads(text).get("ok") is False
    except (ValueError, AttributeError):
        failed = False  # skill text, not a JSON result
    return text, failed


def _check_mcp_version() -> None:
    v = version("mcp")
    major = re.match(r"\d+", v)
    if not major or int(major.group()) < MIN_MCP_MAJOR:
        raise RuntimeError(f"mcp {v} is installed but live-artifacts mcp needs mcp>={MIN_MCP_MAJOR}")


def _load_sdk():
    # Imported here: `mcp` ships in the Hermes venv but is not a dependency of this package.
    _check_mcp_version()
    import mcp_types as types  # noqa: PLC0415
    from mcp.server.lowlevel import Server  # noqa: PLC0415
    from mcp.server.stdio import stdio_server  # noqa: PLC0415

    return types, Server, stdio_server


async def serve(sdk=None) -> None:
    types, Server, stdio_server = sdk or _load_sdk()
    handlers = _handlers()

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(name=s["name"], description=s["description"], input_schema=s["parameters"])
                for s, _ in handlers.values()
            ]
        )

    async def on_list_resources(ctx, params) -> types.ListResourcesResult:
        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    name=f"live-artifacts:{n}",
                    uri=RESOURCE_PREFIX + n,
                    description=_skill_description(n),
                    mime_type="text/markdown",
                )
                for n in _skill_names()
            ]
        )

    async def on_read_resource(ctx, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        text = read_skill(str(params.uri))
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=str(params.uri), mime_type="text/markdown", text=text)]
        )

    async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        text, is_error = await asyncio.to_thread(call, handlers, params.name, params.arguments)
        return types.CallToolResult(content=[types.TextContent(text=text)], is_error=is_error)

    server = Server(
        "live-artifacts",
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_read_resource=on_read_resource,
    )
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def cmd_mcp(args: argparse.Namespace) -> int:
    # Artifacts made over MCP must never adopt a Hermes chat session inherited from the parent environment.
    os.environ.pop("HERMES_SESSION_KEY", None)
    try:
        sdk = _load_sdk()
    except (ImportError, RuntimeError) as e:
        print(
            f"live-artifacts mcp: cannot start ({e}). It needs the `mcp` package (>={MIN_MCP_MAJOR}); "
            "run it with Hermes' Python.",
            file=sys.stderr,
        )
        return 1
    try:
        asyncio.run(serve(sdk))
    except KeyboardInterrupt:
        pass
    return 0
