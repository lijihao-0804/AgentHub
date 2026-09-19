"""Build identity adapters used by formal evaluation persistence."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Protocol

_BUILD_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")


class BuildIdentityProvider(Protocol):
    def get_build_sha(self) -> str | None:
        """Return a validated immutable build identity, or ``None``."""


class EnvironmentBuildIdentityProvider:
    """Resolve an explicit build SHA before falling back to the local Git checkout."""

    def __init__(self, *, repository_root: Path | None = None) -> None:
        self.repository_root = repository_root or Path(__file__).resolve().parents[2]

    def get_build_sha(self) -> str | None:
        if "AGENTHUB_BUILD_SHA" in os.environ:
            return _normalize_sha(os.environ.get("AGENTHUB_BUILD_SHA"))
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repository_root,
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return _normalize_sha(result.stdout)


class StaticBuildIdentityProvider:
    """Test adapter that makes build identity deterministic without Git or environment state."""

    def __init__(self, build_sha: str | None) -> None:
        self.build_sha = build_sha

    def get_build_sha(self) -> str | None:
        return _normalize_sha(self.build_sha)


def _normalize_sha(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized if _BUILD_SHA_PATTERN.fullmatch(normalized) else None


__all__ = [
    "BuildIdentityProvider",
    "EnvironmentBuildIdentityProvider",
    "StaticBuildIdentityProvider",
]
