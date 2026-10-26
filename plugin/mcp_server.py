"""Stdio MCP server exposing the live_artifact_* tools (``hermes live-artifacts mcp``).

Serves the same handlers and schemas as the in-Hermes tools, so Claude Code (or any MCP client) on the same machine shares the
artifact store with the gateway. See specs/claude-code-mcp.md.

stdout carries JSON-RPC only; anything else must go to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

from .server import tools

SKILLS_DIR = Path(__file__).parent / "skills"

# Hermes agents load skills with `skill_view`; MCP clients have no such tool, so the skills are served by one tool instead.
_SKILL_VIEW_SENTENCE = re.compile(r"Before writing the html, call `skill_view\([^`]*`[^.]*?\. ")
_SKILL_POINTER = (
    'Before writing the html, call `live_artifact_skill` with name "capabilities" (the page-side db API) and, for '
    'layout and theming, name "design". '
)


def _skill_names() -> list[str]:
    return sorted(p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md"))


def live_artifact_skill(args: dict, **_kw) -> str:
    name = args.get("name")
    md = SKILLS_DIR / str(name) / "SKILL.md"
    if name not in _skill_names():
        return json.dumps({"ok": False, "error": f"unknown skill {name!r}; available: {_skill_names()}"})
    return md.read_text(encoding="utf-8")


def _skill_schema() -> dict:
    return tools._schema(
        "live_artifact_skill",
        "Read a guide for writing live artifact pages: `capabilities` (the window.claude db / notify API a page uses "
        "to store and return input) or `design` (layout, theming, what the page frame provides). Read before writing html.",
        {"name": {"type": "string", "enum": _skill_names()}},
        ["name"],
    )


def _handlers() -> dict:
    out = {}
    for schema, handler in [*tools.TOOLS, (_skill_schema(), live_artifact_skill)]:
        if schema["name"] == "live_artifact_create":
            schema = {**schema, "description": _SKILL_VIEW_SENTENCE.sub(_SKILL_POINTER, schema["description"])}
        out[schema["name"]] = (schema, handler)
    return out


async def serve() -> None:
    # Imported here: `mcp` ships in the Hermes venv but is not a dependency of this package.
    import mcp_types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    handlers = _handlers()

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(name=s["name"], description=s["description"], input_schema=s["parameters"])
                for s, _ in handlers.values()
            ]
        )

    async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        if params.name not in handlers:
            return types.CallToolResult(content=[types.TextContent(text=f"unknown tool {params.name}")], is_error=True)
        _, handler = handlers[params.name]
        out = await asyncio.to_thread(handler, params.arguments or {})
        return types.CallToolResult(content=[types.TextContent(text=out)])

    server = Server("live-artifacts", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def cmd_mcp(args: argparse.Namespace) -> int:
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    except ImportError as e:
        print(f"live-artifacts mcp needs the `mcp` package (run it with Hermes' Python): {e}", file=sys.stderr)
        return 1
    return 0
