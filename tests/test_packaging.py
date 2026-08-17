"""What a packaged build does differently.

Every assertion here is a failure that cannot happen from source and is invisible until someone
runs the .exe: the database landing in a temp folder that is wiped on exit, auto-start
registered against a `main.py` that is not in the build, resources looked for beside a module
that no longer exists on disk.
"""

from __future__ import annotations

import html
import importlib
import sys
from pathlib import Path

import pytest

from ota_analytics import cli, config, startup


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """Pretend to be a PyInstaller one-folder build living in tmp_path.

    `config.ROOT` is resolved once at import, so any test that reloads the module under this
    fixture leaves it pointing at a temp folder. The patches are therefore undone *before* the
    final reload — otherwise the fake paths leak into every test that runs afterwards, which is
    exactly what happened the first time this was written.
    """
    exe = tmp_path / "InTouchOTA-Analytics.exe"
    exe.write_bytes(b"MZ")
    internal = tmp_path / "_internal"
    internal.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))

    yield tmp_path

    monkeypatch.undo()
    importlib.reload(config)
    assert not config.is_frozen()


# ─── where data goes ────────────────────────────────────────────────────────

def test_data_lives_next_to_the_executable(frozen, monkeypatch):
    """The one that would have silently emptied the database on every launch.

    Deriving paths from __file__ puts them inside PyInstaller's extraction folder, which for a
    one-file build is temporary. The app would have started from nothing every time, and the
    only symptom is a dashboard that is always empty.
    """
    monkeypatch.delenv("OTA_DATA_DIR", raising=False)
    monkeypatch.delenv("OTA_DB_PATH", raising=False)
    reloaded = importlib.reload(config)

    assert reloaded.is_frozen() is True
    assert reloaded.ROOT == frozen
    assert reloaded.DB_PATH == frozen / "data" / "ota_analytics.db"
    assert reloaded.EXPORT_DIR == frozen / "Sample data"
    # And nothing points into the bundle, which is read-only and may be temporary.
    assert str(reloaded.DB_PATH).startswith(str(frozen))
    assert "_internal" not in str(reloaded.DB_PATH)


def test_the_data_dir_override_still_wins(frozen, monkeypatch):
    """A deployment that sets OTA_DATA_DIR must not be overridden by the portable default."""
    monkeypatch.setenv("OTA_DATA_DIR", str(frozen / "elsewhere"))
    assert importlib.reload(config).DATA_DIR == frozen / "elsewhere"


def test_resources_are_read_from_the_bundle(frozen):
    """Read-only files ship inside the bundle; the database does not."""
    assert config.resource("ota_analytics", "schema.sql") == \
        frozen / "_internal" / "ota_analytics" / "schema.sql"


def test_resources_resolve_from_source_without_a_bundle():
    """The same call has to keep working in a checkout, or every test here is meaningless."""
    assert config.resource("ota_analytics", "schema.sql").exists()
    assert config.resource("ota_analytics", "web", "templates", "base.html").exists()
    assert config.is_frozen() is False


# ─── auto-start ─────────────────────────────────────────────────────────────

def test_auto_start_runs_the_executable_not_a_script(frozen):
    """Packaged, there is no main.py — registering one arms auto-start against nothing."""
    command = startup.launch_command()
    assert command[0].endswith(".exe")
    assert not any(part.endswith("main.py") for part in command)
    assert "--no-browser" in command


def test_auto_start_prefers_the_windowless_executable(frozen):
    """Otherwise every reboot leaves a console window sitting on the desktop."""
    assert startup.launch_command()[0].endswith("InTouchOTA-Analytics.exe")

    (frozen / startup.SILENT_EXE).write_bytes(b"MZ")
    assert startup.launch_command()[0].endswith(startup.SILENT_EXE)


def test_auto_start_from_source_still_launches_main_py():
    command = startup.launch_command()
    assert command[-2].endswith("main.py")
    assert command[-1] == "--no-browser"


@pytest.mark.parametrize("folder", ["Program Files/InTouch OTA", "plain", "R&D/tools"])
def test_the_startup_entry_always_quotes_the_program_path(frozen, monkeypatch, tmp_path, folder):
    """Whoever unpacks the app chooses where it lives, and an unquoted path with a space or an
    ampersand is split by the shell into a command that does not exist — silently, at boot."""
    spaced = tmp_path.joinpath(*folder.split("/"))
    spaced.mkdir(parents=True)
    exe = spaced / "InTouchOTA-Analytics.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(startup, "startup_dir", lambda: tmp_path / "Startup")
    monkeypatch.setattr(startup, "is_supported", lambda: True)
    monkeypatch.setattr(startup, "create_task", lambda delay: False)
    monkeypatch.setattr(startup, "task_exists", lambda: False)

    startup.enable(30)
    body = (tmp_path / "Startup" / startup.ENTRY_NAME).read_text(encoding="utf-8")
    run_line = [line for line in body.splitlines() if ".Run " in line][0]
    assert f'""{exe}""' in run_line              # quoted, doubled for the VBScript literal
    assert "--no-browser" in run_line

    # And status() reads back exactly the program it armed, whatever the path looks like.
    monkeypatch.setattr(startup, "_environment_warning", lambda: "")
    assert startup._armed_target("startup-folder") == str(exe)
    assert startup.status().warning == ""


def test_a_moved_app_is_reported_rather_than_silently_dead(monkeypatch, tmp_path):
    """Portable data means the folder gets moved, and the entry keeps pointing at the old path.

    Nothing fails loudly when that happens: the machine simply stops collecting, and the gap is
    noticed days later when a trend looks wrong.
    """
    startup_folder = tmp_path / "Startup"
    startup_folder.mkdir()
    monkeypatch.setattr(startup, "startup_dir", lambda: startup_folder)
    monkeypatch.setattr(startup, "is_supported", lambda: True)
    monkeypatch.setattr(startup, "create_task", lambda delay: False)
    monkeypatch.setattr(startup, "task_exists", lambda: False)
    monkeypatch.setattr(startup, "_environment_warning", lambda: "")

    startup.enable(30)
    assert startup.status().warning == ""        # armed against the current location

    # Now the app moves somewhere else.
    moved = tmp_path / "moved" / "InTouchOTA-Analytics.exe"
    moved.parent.mkdir()
    moved.write_bytes(b"MZ")
    monkeypatch.setattr(startup, "launch_command",
                        lambda: [str(moved), "--no-browser"])

    warning = startup.status().warning
    assert warning
    assert "moved" in warning.lower() or "different copy" in warning.lower()


# ─── what the first screen tells someone to do ──────────────────────────────

def test_the_setup_hint_names_the_executable_when_packaged(frozen):
    """The empty dashboard is the first thing a new install shows.

    Telling someone to run `python -m ota_analytics.cli` in a build that has no Python sends
    them off to install one to fix a problem they do not have.
    """
    importlib.reload(config)
    hint = config.command_hint("ingest-dir", str(config.EXPORT_DIR))
    assert hint.startswith("InTouchOTA-Analytics.exe")
    assert "python" not in hint
    assert "ota_analytics.cli" not in hint


def test_the_setup_hint_uses_python_from_source():
    hint = config.command_hint("ingest-dir", "Sample data")
    assert hint == 'python -m ota_analytics.cli ingest-dir "Sample data"'


def test_the_empty_dashboard_offers_the_right_command(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ota_analytics import db, scheduler, sources

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "empty.db")
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "Sample data")
    monkeypatch.setattr(scheduler, "STATE_PATH", tmp_path / "scheduler.json")
    monkeypatch.setattr(sources, "SETTINGS_PATH", tmp_path / "connection.json")
    monkeypatch.setattr(scheduler, "_scheduler", None)
    db.connect()

    from ota_analytics import api
    response = TestClient(api.app, raise_server_exceptions=False).get("/")

    assert response.status_code == 200
    assert "No data yet" in response.text
    assert "Something went wrong" not in response.text
    # It points at the page that actually does the work, not only at a shell command.
    assert 'href="/update"' in response.text
    # Unescaped, because the quotes around a path with spaces are rendered as entities — which
    # is the template doing the right thing, not the command being wrong.
    rendered = html.unescape(response.text)
    assert config.command_hint("ingest-dir", str(config.EXPORT_DIR)) in rendered


# ─── the CLI, reachable from the executable ─────────────────────────────────

def test_the_executable_exposes_the_cli():
    """Otherwise sharing a database and setting a password become source-only features."""
    names = cli.command_names()
    for command in ("db-info", "db-export", "db-import", "passwd", "ingest", "rollup"):
        assert command in names, f"{command} is unreachable from the packaged build"


def test_main_delegates_a_cli_command(monkeypatch):
    import main as entry

    called = {}

    def fake_cli(argv):
        called["argv"] = argv
        return 0

    monkeypatch.setattr("ota_analytics.cli.main", fake_cli)
    assert entry.main(["db-info"]) == 0
    assert called["argv"] == ["db-info"]


def test_main_still_treats_dashboard_flags_as_its_own(monkeypatch):
    """`--port 9000` must not be mistaken for a CLI subcommand."""
    import main as entry

    monkeypatch.setattr("ota_analytics.cli.main",
                        lambda argv: pytest.fail("dashboard flags went to the CLI"))
    monkeypatch.setattr(entry, "background_status", lambda: 7)
    assert entry.main(["--status"]) == 7


# ─── one app, one copy ──────────────────────────────────────────────────────

def test_the_app_marker_matches_what_the_server_reports():
    """Two constants, one meaning: if they drift, every launch starts a second copy again."""
    import main as entry

    from ota_analytics import api
    assert entry.APP_MARKER == api.APP_ID


def test_healthz_identifies_the_app_without_revealing_data():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ota_analytics import api
    payload = TestClient(api.app).get("/healthz").json()

    assert payload["app"] == api.APP_ID
    assert payload["status"] == "ok"
    # It is public and unauthenticated, so it must stay free of anything about the fleet.
    assert set(payload) == {"status", "app"}


def test_a_second_launch_opens_the_running_copy(monkeypatch):
    """Auto-start leaves a windowless copy holding the port. Double-clicking the app then
    started a second one on 8001, with its own scheduler and no way to tell them apart."""
    import main as entry

    opened = {}
    monkeypatch.setattr(entry, "already_serving", lambda host, port, **kw: True)
    monkeypatch.setattr(entry.webbrowser, "open", lambda url: opened.setdefault("url", url))
    monkeypatch.setattr(entry, "load_new_exports",
                        lambda: pytest.fail("a second copy started anyway"))

    assert entry.main(["--port", "8000"]) == 0
    assert opened["url"] == "http://127.0.0.1:8000"


def test_nothing_listening_is_not_mistaken_for_our_app(monkeypatch):
    """A closed port must read as 'not us' rather than raise on the way to starting up."""
    import main as entry

    assert entry.already_serving("127.0.0.1", 59999, timeout=0.2) is False


def test_a_port_held_by_something_else_still_falls_back(monkeypatch):
    """Only our own dashboard is deferred to; an unrelated service keeps the old behaviour
    of quietly moving to the next free port."""
    import http.server
    import threading

    import main as entry

    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert entry.already_serving("127.0.0.1", server.server_port, timeout=2.0) is False
    finally:
        server.shutdown()
        server.server_close()


# ─── the build definition itself ────────────────────────────────────────────

def test_the_spec_ships_every_runtime_resource():
    """Nothing imports these, so PyInstaller cannot find them by analysis."""
    spec = Path(__file__).resolve().parent.parent / "InTouchOTA-Analytics.spec"
    body = spec.read_text(encoding="utf-8")
    for needed in ("schema.sql", "web/templates", "web/static"):
        assert needed in body, f"{needed} would be missing from the build"
    # Imported by name at runtime; without these the build runs and then fails.
    for needed in ("uvicorn.loops.auto", "uvicorn.protocols.http.auto",
                   "keyring.backends.Windows"):
        assert needed in body, f"{needed} would be missing and the exe would fail at runtime"


def test_the_spec_builds_both_executables():
    spec = Path(__file__).resolve().parent.parent / "InTouchOTA-Analytics.spec"
    body = spec.read_text(encoding="utf-8")
    assert "console=True" in body and "console=False" in body
    assert f'"{startup.SILENT_EXE[:-4]}"' in body or startup.SILENT_EXE[:-4] in body
