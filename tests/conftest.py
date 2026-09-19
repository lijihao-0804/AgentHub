"""Test-process runtime setup kept outside production module imports."""

from packages.agent_runtime.adapters.langgraph import configure_windows_asyncio_policy


def pytest_configure() -> None:
    configure_windows_asyncio_policy()
