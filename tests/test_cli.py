"""Unit tests for hermes live-artifacts CLI commands."""

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest

from live_artifacts import cli
from live_artifacts.server import tools


@pytest.fixture
def cli_store(tools_store):
    tools.configure("http://127.0.0.1:47318", link_secret=True)
    return tools_store


@pytest.fixture
def parser():
    p = argparse.ArgumentParser()
    cli.setup_cli_parser(p)
    return p


def test_register_cli():
    ctx = MagicMock()
    cli.register_cli(ctx)
    ctx.register_cli_command.assert_called_once()
    name = ctx.register_cli_command.call_args.kwargs["name"]
    assert name == "live-artifacts"


def test_list_empty(cli_store, parser, capsys):
    args = parser.parse_args(["list"])
    rc = cli.live_artifacts_main(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "No artifacts found" in out


def test_list_json(cli_store, parser, capsys):
    a = cli_store.create_artifact("Test Item", "<p>hi</p>", {})
    args = parser.parse_args(["list", "--json"])
    rc = cli.live_artifacts_main(args)
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["id"] == a["id"]
    assert data[0]["title"] == "Test Item"
    assert data[0]["status"] == "active"


def test_list_filter_status(cli_store, parser, capsys):
    a1 = cli_store.create_artifact("Active Item", "<p>hi</p>", {})
    a2 = cli_store.create_artifact("Archived Item", "<p>hi</p>", {})
    cli_store.archive(a2["id"])

    # Active filter
    args = parser.parse_args(["list", "--status", "active", "--json"])
    rc = cli.live_artifacts_main(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["id"] == a1["id"]

    # Archived filter
    args = parser.parse_args(["list", "--status", "archived", "--json"])
    rc = cli.live_artifacts_main(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["id"] == a2["id"]


def test_show_metadata(cli_store, parser, capsys):
    a = cli_store.create_artifact("Item Details", "<p>hi</p>", {}, origin_session="sess-123")
    args = parser.parse_args(["show", a["id"]])
    rc = cli.live_artifacts_main(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert a["id"] in out
    assert "Item Details" in out
    assert "sess-123" in out


def test_show_with_data(cli_store, parser, capsys):
    a = cli_store.create_artifact("Form", "<p>hi</p>", {})
    cli_store.set_doc(a["id"], "answers/q1", {"answer": "blue"})
    args = parser.parse_args(["show", a["id"], "--data"])
    rc = cli.live_artifacts_main(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "answers/q1" in out
    assert "blue" in out


def test_show_json(cli_store, parser, capsys):
    a = cli_store.create_artifact("JSON Form", "<p>hi</p>", {})
    cli_store.set_doc(a["id"], "answers/q1", {"answer": 42})
    args = parser.parse_args(["show", a["id"], "--json"])
    rc = cli.live_artifacts_main(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["artifact"]["id"] == a["id"]
    assert data["docs"]["answers/q1"] == {"answer": 42}


def test_show_missing_id(cli_store, parser, capsys):
    args = parser.parse_args(["show", "non-existent-id"])
    rc = cli.live_artifacts_main(args)
    assert rc != 0
    err = capsys.readouterr().err
    assert "No such artifact" in err


def test_link_active(cli_store, parser, capsys):
    a = cli_store.create_artifact("Linked", "<p>hi</p>", {})
    args = parser.parse_args(["link", a["id"]])
    rc = cli.live_artifacts_main(args)
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("http://127.0.0.1:47318/a/")
    assert f"?k={a['token']}" in out


def test_link_rotate(cli_store, parser, capsys):
    a = cli_store.create_artifact("Rotatable", "<p>hi</p>", {})
    old_token = a["token"]
    args = parser.parse_args(["link", a["id"], "--rotate"])
    rc = cli.live_artifacts_main(args)
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert old_token not in out
    # Verify DB token updated
    updated = cli_store.get_artifact(a["id"])
    assert updated["token"] in out
    assert updated["token"] != old_token


def test_link_rotate_disabled(cli_store, parser, capsys):
    tools.configure("http://127.0.0.1:47318", link_secret=False)
    a = cli_store.create_artifact("NoSecret", "<p>hi</p>", {})
    args = parser.parse_args(["link", a["id"], "--rotate"])
    rc = cli.live_artifacts_main(args)
    assert rc != 0
    err = capsys.readouterr().err
    assert "link rotation is meaningless with link_secret=never" in err


def test_link_archived_refuses(cli_store, parser, capsys):
    a = cli_store.create_artifact("ToArchive", "<p>hi</p>", {})
    cli_store.archive(a["id"])
    args = parser.parse_args(["link", a["id"]])
    rc = cli.live_artifacts_main(args)
    assert rc != 0
    err = capsys.readouterr().err
    assert "is archived" in err


def test_open_active(cli_store, parser, capsys):
    a = cli_store.create_artifact("ToOpen", "<p>hi</p>", {})
    args = parser.parse_args(["open", a["id"]])
    with patch("webbrowser.open") as mock_open:
        rc = cli.live_artifacts_main(args)
        assert rc == 0
        mock_open.assert_called_once()
        assert f"/a/{a['id']}" in mock_open.call_args[0][0]


def test_open_archived_fails(cli_store, parser, capsys):
    a = cli_store.create_artifact("ArchivedOpen", "<p>hi</p>", {})
    cli_store.archive(a["id"])
    args = parser.parse_args(["open", a["id"]])
    with patch("webbrowser.open") as mock_open:
        rc = cli.live_artifacts_main(args)
        assert rc != 0
        mock_open.assert_not_called()
    err = capsys.readouterr().err
    assert "Cannot open archived artifact" in err


def test_archive(cli_store, parser, capsys):
    a = cli_store.create_artifact("WillArchive", "<p>hi</p>", {})
    args = parser.parse_args(["archive", a["id"]])
    rc = cli.live_artifacts_main(args)
    assert rc == 0
    assert cli_store.get_artifact(a["id"])["status"] == "archived"


def test_archive_missing(cli_store, parser, capsys):
    args = parser.parse_args(["archive", "missing-id"])
    rc = cli.live_artifacts_main(args)
    assert rc != 0
    err = capsys.readouterr().err
    assert "No such artifact" in err


def test_delete_with_yes(cli_store, parser, capsys):
    a = cli_store.create_artifact("WillDelete", "<p>hi</p>", {})
    cli_store.set_doc(a["id"], "doc/1", {"val": 1})
    args = parser.parse_args(["delete", a["id"], "--yes"])
    rc = cli.live_artifacts_main(args)
    assert rc == 0
    assert cli_store.get_artifact(a["id"]) is None
    assert cli_store.dump_docs(a["id"]) == {}


def test_delete_missing(cli_store, parser, capsys):
    args = parser.parse_args(["delete", "missing-id", "--yes"])
    rc = cli.live_artifacts_main(args)
    assert rc != 0
    err = capsys.readouterr().err
    assert "No such artifact" in err


def test_delete_tty_prompt_abort(cli_store, parser, capsys, monkeypatch):
    a = cli_store.create_artifact("WillPrompt", "<p>hi</p>", {})
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    args = parser.parse_args(["delete", a["id"]])
    rc = cli.live_artifacts_main(args)
    assert rc != 0
    assert cli_store.get_artifact(a["id"]) is not None


def test_delete_tty_prompt_accept(cli_store, parser, capsys, monkeypatch):
    a = cli_store.create_artifact("WillPromptAccept", "<p>hi</p>", {})
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    args = parser.parse_args(["delete", a["id"]])
    rc = cli.live_artifacts_main(args)
    assert rc == 0
    assert cli_store.get_artifact(a["id"]) is None


def test_export_stdout(cli_store, parser, capsys):
    a = cli_store.create_artifact("ExportItem", "<p>exported</p>", {})
    cli_store.set_doc(a["id"], "doc/1", {"hello": "world"})
    args = parser.parse_args(["export", a["id"]])
    rc = cli.live_artifacts_main(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["id"] == a["id"]
    assert data["html"] == "<p>exported</p>"
    assert data["docs"]["doc/1"] == {"hello": "world"}


def test_export_file(cli_store, parser, tmp_path, capsys):
    a = cli_store.create_artifact("ExportFile", "<p>exported</p>", {})
    out_file = tmp_path / "subdir" / "exported.json"
    args = parser.parse_args(["export", a["id"], "--out", str(out_file)])
    rc = cli.live_artifacts_main(args)
    assert rc == 0
    assert out_file.exists()
    data = json.loads(out_file.read_text())
    assert data["id"] == a["id"]


def test_export_missing(cli_store, parser, capsys):
    args = parser.parse_args(["export", "missing-id"])
    rc = cli.live_artifacts_main(args)
    assert rc != 0
    err = capsys.readouterr().err
    assert "No such artifact" in err


def test_index_link_refuses_when_off(cli_store, parser, capsys):
    tools.configure("http://127.0.0.1:47318", index=False)
    rc = cli.live_artifacts_main(parser.parse_args(["index-link"]))
    assert rc != 0
    assert "index page is off" in capsys.readouterr().err


def test_index_link_prints_and_rotates(cli_store, parser, capsys):
    tools.configure("http://127.0.0.1:47318", index=True)
    assert cli.live_artifacts_main(parser.parse_args(["index-link"])) == 0
    first = capsys.readouterr().out.strip()
    assert first == f"http://127.0.0.1:47318/index?k={cli_store.index_token()}"
    assert cli.live_artifacts_main(parser.parse_args(["index-link", "--rotate"])) == 0
    assert capsys.readouterr().out.strip() != first
