"""``Settings`` built from the declared defaults rather than from the machine.

``Settings`` reads ``.env`` and the ``AGENTHUB_*`` process variables on
purpose: that is how a deployment configures it, and a test that exercises
configured behaviour should keep reading them. A test that asserts what a
setting *defaults to* must not, or it ends up asserting whatever the developer
happens to have in their ``.env`` -- passing on a clean checkout and failing on
a working one, which is the wrong way round.

Both sources are shut off here, not just the file: an exported
``AGENTHUB_MCP_ALLOW_PRIVATE_TARGETS`` outranks the dotenv and would reopen the
same hole.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from packages.core.config.settings import Settings

_ENV_PREFIX = str(Settings.model_config.get("env_prefix", ""))


@contextmanager
def _without_ambient_environment() -> Iterator[None]:
    hidden = {key: value for key, value in os.environ.items() if key.startswith(_ENV_PREFIX)}
    for key in hidden:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(hidden)


def declared_settings(**overrides: object) -> Settings:
    """Settings as the code declares them, plus the overrides given here."""

    with _without_ambient_environment():
        return Settings(_env_file=None, **overrides)
