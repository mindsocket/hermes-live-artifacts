"""CLI commands for the live-artifacts plugin (``hermes live-artifacts <subcommand>``).

Commands:
  list      — List artifacts (active / archived)
  show      — Show artifact metadata and documents
  link      — Display or rotate artifact deep link
  index-link — Display or rotate the artifact index link (needs the `index` setting)
  open      — Open artifact in the default browser
  archive   — Archive an artifact
  delete    — Delete an artifact and its documents
  export    — Export artifact bundle to JSON
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .server import admin, tools
from .server.store import Store

console = Console()
console_err = Console(stderr=True)


def _get_store() -> Store:
    return tools.store()


def _format_time(ts: float | None) -> str:
    if not ts:
        return "-"
    try:
        dt = datetime.fromtimestamp(ts, tz=UTC).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


# ---------------------------------------------------------------- list
def cmd_list(args: argparse.Namespace) -> int:
    st = _get_store()
    data = admin.list_artifacts(st, status=args.status)
    artifacts = data.get("artifacts", [])

    if args.json:
        print(json.dumps(artifacts, indent=2))
        return 0

    if not artifacts:
        status_msg = f" with status '{args.status}'" if args.status else ""
        console.print(f"[dim]No artifacts found{status_msg}.[/dim]")
        return 0

    table = Table(title="Live Artifacts", show_lines=False)
    table.add_column("ID", style="cyan bold", no_wrap=True)
    table.add_column("Title", style="white")
    table.add_column("Status", style="bold")
    table.add_column("Pinned", justify="center")
    table.add_column("Docs", justify="right")
    table.add_column("Created", style="dim")
    table.add_column("Last Update", style="dim")

    for a in artifacts:
        status_styled = "[green]active[/green]" if a.get("status") == "active" else "[dim]archived[/dim]"
        pinned_styled = "[yellow]★[/yellow]" if a.get("pinned") else "[dim]—[/dim]"
        table.add_row(
            str(a.get("id", "")),
            str(a.get("title", "")),
            status_styled,
            pinned_styled,
            str(a.get("doc_count", 0)),
            _format_time(a.get("created_at")),
            _format_time(a.get("updated_at")),
        )

    console.print(table)
    return 0


# ---------------------------------------------------------------- show
def cmd_show(args: argparse.Namespace) -> int:
    st = _get_store()
    aid = args.id
    try:
        d = admin.detail(st, aid)
    except KeyError:
        console_err.print(f"[red]Error:[/red] No such artifact: {aid}")
        return 1

    if args.json:
        print(json.dumps(d, indent=2))
        return 0

    art = d.get("artifact", {})
    status_str = "[green]active[/green]" if art.get("status") == "active" else "[dim red]archived[/dim red]"
    pinned_str = "[yellow]yes[/yellow]" if art.get("pinned") else "no"

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan")
    grid.add_column(style="white")

    grid.add_row("ID:", art.get("id", ""))
    grid.add_row("Title:", art.get("title", ""))
    grid.add_row("Status:", status_str)
    grid.add_row("Pinned:", pinned_str)
    grid.add_row("Origin Session:", art.get("origin_session") or "[dim]none[/dim]")
    grid.add_row("Documents:", str(art.get("doc_count", 0)))
    grid.add_row("Created:", _format_time(art.get("created_at")))
    grid.add_row("Last Update:", _format_time(art.get("updated_at")))
    if art.get("notified_at"):
        grid.add_row("Last Notified:", _format_time(art.get("notified_at")))
    grid.add_row("Link:", d.get("link") or "[dim red](archived / inactive)[/dim red]")

    console.print(Panel(grid, title=f"Artifact: {art.get('id')}", expand=False))

    if args.data:
        docs = d.get("docs", {})
        if not docs:
            console.print("[dim]No saved documents.[/dim]")
        else:
            doc_table = Table(title="Saved Documents", show_lines=True)
            doc_table.add_column("Path", style="cyan bold")
            doc_table.add_column("Data", style="white")
            for path, content in sorted(docs.items()):
                doc_table.add_row(path, json.dumps(content, indent=2))
            console.print(doc_table)

    return 0


# ---------------------------------------------------------------- link
def cmd_link(args: argparse.Namespace) -> int:
    st = _get_store()
    aid = args.id
    if args.rotate:
        try:
            new_link = admin.rotate_link(st, aid)
        except KeyError:
            console_err.print(f"[red]Error:[/red] No such artifact: {aid}")
            return 1
        except ValueError as e:
            console_err.print(f"[red]Error:[/red] {e}")
            return 1

        if args.json:
            print(json.dumps({"id": aid, "link": new_link, "rotated": True}, indent=2))
        else:
            print(new_link)
        return 0

    try:
        d = admin.detail(st, aid)
    except KeyError:
        console_err.print(f"[red]Error:[/red] No such artifact: {aid}")
        return 1

    link_url = d.get("link")
    if not link_url:
        console_err.print(f"[red]Error:[/red] Artifact {aid} is archived (links are inactive)")
        return 1

    if args.json:
        print(json.dumps({"id": aid, "link": link_url}, indent=2))
    else:
        print(link_url)
    return 0


# ---------------------------------------------------------------- index-link
def cmd_index_link(args: argparse.Namespace) -> int:
    if not tools.index_enabled():
        console_err.print("[red]Error:[/red] the index page is off; set the `index` plugin setting to true")
        return 1
    st = _get_store()
    token = st.rotate_index_token() if args.rotate else st.index_token()
    link_url = f"{tools.base_url()}/index?k={token}"
    if args.json:
        print(json.dumps({"link": link_url, **({"rotated": True} if args.rotate else {})}, indent=2))
    else:
        print(link_url)
    return 0


# ---------------------------------------------------------------- open
def cmd_open(args: argparse.Namespace) -> int:
    st = _get_store()
    aid = args.id
    try:
        d = admin.detail(st, aid)
    except KeyError:
        console_err.print(f"[red]Error:[/red] No such artifact: {aid}")
        return 1

    link_url = d.get("link")
    if not link_url:
        console_err.print(f"[red]Error:[/red] Cannot open archived artifact: {aid}")
        return 1

    try:
        webbrowser.open(link_url)
        console.print(f"[green]Opened:[/green] {link_url}")
        return 0
    except Exception as e:
        console_err.print(f"[red]Error opening browser:[/red] {e}")
        return 1


# ---------------------------------------------------------------- archive
def cmd_archive(args: argparse.Namespace) -> int:
    st = _get_store()
    aid = args.id
    try:
        st.archive(aid)
    except KeyError:
        console_err.print(f"[red]Error:[/red] No such artifact: {aid}")
        return 1

    if args.json:
        print(json.dumps({"ok": True, "id": aid, "status": "archived"}, indent=2))
    else:
        console.print(f"[green]Archived artifact [bold]{aid}[/bold].[/green]")
    return 0


# ---------------------------------------------------------------- delete
def cmd_delete(args: argparse.Namespace) -> int:
    st = _get_store()
    aid = args.id

    # Verify existence first
    if st.get_artifact(aid) is None:
        console_err.print(f"[red]Error:[/red] No such artifact: {aid}")
        return 1

    if not args.yes:
        if sys.stdin.isatty() and sys.stdout.isatty():
            try:
                ans = input(f"Delete artifact {aid} and all its documents? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans not in ("y", "yes"):
                console_err.print("Aborted.")
                return 1

    try:
        st.delete(aid)
    except KeyError:
        console_err.print(f"[red]Error:[/red] No such artifact: {aid}")
        return 1

    if args.json:
        print(json.dumps({"ok": True, "id": aid, "deleted": True}, indent=2))
    else:
        console.print(f"[green]Deleted artifact [bold]{aid}[/bold].[/green]")
    return 0


# ---------------------------------------------------------------- export
def cmd_export(args: argparse.Namespace) -> int:
    st = _get_store()
    aid = args.id
    try:
        data = admin.export_one(st, aid)
    except KeyError:
        console_err.print(f"[red]Error:[/red] No such artifact: {aid}")
        return 1

    out_json = json.dumps(data, indent=2)
    if args.out:
        out_path = Path(args.out).expanduser().resolve()
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(out_json, encoding="utf-8")
            console.print(f"[green]Exported artifact [bold]{aid}[/bold] to {out_path}[/green]")
        except OSError as e:
            console_err.print(f"[red]Error writing to {out_path}:[/red] {e}")
            return 1
    else:
        print(out_json)
    return 0


# ---------------------------------------------------------------- CLI registration
def setup_cli_parser(parser: argparse.ArgumentParser) -> None:
    """Build the ``hermes live-artifacts`` argparse subcommand tree."""
    subs = parser.add_subparsers(dest="live_artifacts_command", required=True)

    # list
    p_list = subs.add_parser("list", help="List artifacts")
    p_list.add_argument(
        "--status", choices=["active", "archived"], default=None, help="Filter by status (default: all)"
    )
    p_list.add_argument("--json", action="store_true", help="Output as JSON")
    p_list.set_defaults(func=cmd_list)

    # show
    p_show = subs.add_parser("show", help="Show artifact metadata and document tree")
    p_show.add_argument("id", help="Artifact ID")
    p_show.add_argument("--data", action="store_true", help="Include saved documents in output")
    p_show.add_argument("--json", action="store_true", help="Output as JSON")
    p_show.set_defaults(func=cmd_show)

    # link
    p_link = subs.add_parser("link", help="Display or rotate artifact deep link")
    p_link.add_argument("id", help="Artifact ID")
    p_link.add_argument("--rotate", action="store_true", help="Rotate capability token and return new link")
    p_link.add_argument("--json", action="store_true", help="Output as JSON")
    p_link.set_defaults(func=cmd_link)

    # index-link
    p_index = subs.add_parser("index-link", help="Display or rotate the artifact index link")
    p_index.add_argument("--rotate", action="store_true", help="Rotate the index token and return the new link")
    p_index.add_argument("--json", action="store_true", help="Output as JSON")
    p_index.set_defaults(func=cmd_index_link)

    # open
    p_open = subs.add_parser("open", help="Open artifact deep link in default browser")
    p_open.add_argument("id", help="Artifact ID")
    p_open.set_defaults(func=cmd_open)

    # archive
    p_archive = subs.add_parser("archive", help="Archive an artifact (one-way)")
    p_archive.add_argument("id", help="Artifact ID")
    p_archive.add_argument("--json", action="store_true", help="Output as JSON")
    p_archive.set_defaults(func=cmd_archive)

    # delete
    p_delete = subs.add_parser("delete", help="Delete an artifact and its documents")
    p_delete.add_argument("id", help="Artifact ID")
    p_delete.add_argument("-y", "--yes", action="store_true", help="Do not prompt for confirmation")
    p_delete.add_argument("--json", action="store_true", help="Output as JSON")
    p_delete.set_defaults(func=cmd_delete)

    # export
    p_export = subs.add_parser("export", help="Export artifact bundle (metadata, HTML, docs) to JSON")
    p_export.add_argument("id", help="Artifact ID")
    p_export.add_argument("--out", help="Write JSON to specified file path instead of stdout")
    p_export.set_defaults(func=cmd_export)


def live_artifacts_main(args: argparse.Namespace) -> int:
    """Top-level handler for ``hermes live-artifacts``."""
    if hasattr(args, "func") and args.func is not live_artifacts_main:
        return int(args.func(args) or 0)
    return 0


def register_cli(ctx: Any) -> None:
    """Register CLI subcommand with Hermes plugin context."""
    if hasattr(ctx, "register_cli_command"):
        ctx.register_cli_command(
            name="live-artifacts",
            help="Manage live artifacts (forms, review lists, dashboards)",
            setup_fn=setup_cli_parser,
            handler_fn=live_artifacts_main,
            description="Manage live artifacts: list, show, link, open, archive, delete, export.",
        )
