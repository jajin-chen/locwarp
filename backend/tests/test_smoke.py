"""Smoke test: pytest infrastructure works and backend modules import."""


def test_config_imports() -> None:
    import config

    assert hasattr(config, "RECONNECT_BASE_DELAY")


async def test_asyncio_mode_auto_works() -> None:
    import asyncio

    await asyncio.sleep(0)
