"""Regression tests for launcher child-process supervision."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


_START_PATH = Path(__file__).parents[2] / "start.py"
_SPEC = importlib.util.spec_from_file_location("locwarp_start", _START_PATH)
assert _SPEC is not None and _SPEC.loader is not None
start = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(start)


@pytest.mark.parametrize("return_code", [0, 17])
def test_wait_for_port_fails_immediately_when_child_exits(
    monkeypatch,
    capsys,
    return_code,
) -> None:
    process = Mock()
    process.poll.return_value = return_code
    sleep = Mock()
    monkeypatch.setattr(start, "is_port_open", lambda _port: False)
    monkeypatch.setattr(start.time, "sleep", sleep)

    assert not start.wait_for_port(8777, "後端", timeout=60, process=process)
    assert f"exit code {return_code}" in capsys.readouterr().out
    sleep.assert_not_called()


def test_start_backend_passes_owned_process_to_port_wait(monkeypatch) -> None:
    process = Mock()
    wait = Mock(return_value=True)
    monkeypatch.setattr(start, "is_port_open", lambda _port: False)
    monkeypatch.setattr(start.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(start, "wait_for_port", wait)
    monkeypatch.setattr(start, "procs", [])

    assert start.start_backend() is process
    wait.assert_called_once_with(start.BACKEND_PORT, "後端", process=process)


def test_start_frontend_watches_backend_during_port_wait(monkeypatch) -> None:
    backend = Mock()
    frontend = Mock()
    wait = Mock(return_value=True)
    monkeypatch.setattr(start, "is_port_open", lambda _port: False)
    monkeypatch.setattr(start.subprocess, "Popen", Mock(return_value=frontend))
    monkeypatch.setattr(start, "wait_for_port", wait)
    monkeypatch.setattr(start, "procs", [])

    assert start.start_frontend(backend) is frontend
    wait.assert_called_once_with(
        start.FRONTEND_PORT,
        "前端",
        process=frontend,
        watched_processes=(("後端", backend),),
    )


@pytest.mark.parametrize("return_code", [0, 23])
def test_wait_for_shutdown_reports_backend_death(
    monkeypatch,
    capsys,
    return_code,
) -> None:
    backend = Mock()
    backend.poll.return_value = return_code
    monkeypatch.setattr(start, "_enter_pressed", lambda: False)

    assert not start.wait_for_shutdown(backend, poll_interval=0)
    assert f"exit code {return_code}" in capsys.readouterr().out


def test_wait_for_shutdown_accepts_enter(monkeypatch) -> None:
    backend = Mock()
    backend.poll.return_value = None
    monkeypatch.setattr(start, "_enter_pressed", lambda: True)

    assert start.wait_for_shutdown(backend, poll_interval=0)


@pytest.mark.parametrize(("clean_exit", "expected_code"), [(True, 0), (False, 1)])
def test_main_cleans_up_and_returns_expected_code(
    monkeypatch,
    clean_exit,
    expected_code,
) -> None:
    backend = Mock()
    frontend = Mock()
    cleanup = Mock()
    wait = Mock(return_value=clean_exit)
    start_frontend = Mock(return_value=frontend)

    monkeypatch.setattr(start.os, "system", lambda _command: 0)
    monkeypatch.setattr(start, "print_banner", lambda: None)
    monkeypatch.setattr(start, "check_admin", lambda: True)
    monkeypatch.setattr(start, "check_tool", lambda *_args: True)
    monkeypatch.setattr(start, "install_backend", lambda: None)
    monkeypatch.setattr(start, "install_frontend", lambda: None)
    monkeypatch.setattr(start, "start_backend", lambda: backend)
    monkeypatch.setattr(start, "start_frontend", start_frontend)
    monkeypatch.setattr(start.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(start.webbrowser, "open", lambda _url: None)
    monkeypatch.setattr(start, "wait_for_shutdown", wait)
    monkeypatch.setattr(start, "cleanup", cleanup)

    assert start.main() == expected_code
    start_frontend.assert_called_once_with(backend)
    wait.assert_called_once_with(backend)
    cleanup.assert_called_once_with()


def test_main_cleans_up_when_backend_fails_to_start(monkeypatch) -> None:
    cleanup = Mock()
    start_frontend = Mock()
    wait = Mock()

    monkeypatch.setattr(start.os, "system", lambda _command: 0)
    monkeypatch.setattr(start, "print_banner", lambda: None)
    monkeypatch.setattr(start, "check_admin", lambda: True)
    monkeypatch.setattr(start, "check_tool", lambda *_args: True)
    monkeypatch.setattr(start, "install_backend", lambda: None)
    monkeypatch.setattr(start, "install_frontend", lambda: None)
    monkeypatch.setattr(start, "start_backend", lambda: None)
    monkeypatch.setattr(start, "start_frontend", start_frontend)
    monkeypatch.setattr(start, "wait_for_shutdown", wait)
    monkeypatch.setattr(start, "cleanup", cleanup)
    monkeypatch.setattr("builtins.input", lambda *_args: "")

    assert start.main() == 1
    cleanup.assert_called_once_with()
    start_frontend.assert_not_called()
    wait.assert_not_called()
