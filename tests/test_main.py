"""Startup behavior of the VS Code entry point."""

from __future__ import annotations

import socket
import time

import main


def test_find_free_port_returns_preferred_when_available():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    assert main.find_free_port("127.0.0.1", free_port) == free_port


def test_find_free_port_steps_past_a_busy_port():
    """A leftover server from an earlier run must not turn into 'address already in use'."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        busy = held.getsockname()[1]
        assert main.find_free_port("127.0.0.1", busy) > busy


def test_browser_opens_once_the_port_accepts(monkeypatch):
    """The browser must open when the server is actually reachable.

    Paired with the never-listens test below, this pins the ordering property that a fixed
    timer got wrong (browser first, server second -> 'refused to connect'). The two are kept
    separate deliberately: asserting "not yet opened" mid-flight is timing-sensitive on
    Windows, where a connect to a bound-but-unlistening port blocks for the full timeout
    instead of being refused, so a loaded test run turns that assertion flaky.
    """
    opened: list[str] = []
    monkeypatch.setattr(main.webbrowser, "open", lambda url: opened.append(url))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        url = f"http://127.0.0.1:{port}"

        main.open_browser_when_ready(url, "127.0.0.1", port, timeout=20)
        deadline = time.monotonic() + 20
        while not opened and time.monotonic() < deadline:
            time.sleep(0.05)

    assert opened == [url]


def test_browser_never_opens_while_nothing_listens(monkeypatch, capsys):
    opened: list[str] = []
    monkeypatch.setattr(main.webbrowser, "open", lambda url: opened.append(url))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]

    main.open_browser_when_ready("http://x", "127.0.0.1", dead_port, timeout=0.5)
    time.sleep(1.5)
    assert opened == []
    assert "manually" in capsys.readouterr().out
