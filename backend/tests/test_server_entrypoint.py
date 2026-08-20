"""Regression tests for the Windows Uvicorn event-loop selection."""

from __future__ import annotations

import asyncio
import uvicorn

import main


def test_server_loop_matches_platform() -> None:
    loop_name = main._server_loop("win32")
    assert loop_name == "asyncio:SelectorEventLoop"

    factory = uvicorn.Config("main:app", loop=loop_name).get_loop_factory()
    assert factory is not None
    loop = factory()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()

    assert main._server_loop("linux") == "auto"


def test_run_server_passes_selected_loop_to_uvicorn(monkeypatch) -> None:
    captured: dict = {}

    def fake_run(*args, **kwargs) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(main.uvicorn, "run", fake_run)

    main._run_server()

    assert captured["args"] == ("main:app",)
    assert captured["kwargs"]["loop"] == main._server_loop()
