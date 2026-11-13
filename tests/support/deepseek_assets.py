"""Resolve the frozen DeepSeek research tokenizer for tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

from packages.agent_runtime.deepseek_v41_counter import (
    MAX_TOKENIZER_BYTES,
    DeepseekV41TextCounter,
)
from packages.agent_runtime.frozen import (
    SUMMARY_NON_THINKING_COUNTER_REVISION,
    SUMMARY_NON_THINKING_TOKENIZER_SHA256,
)

TOKENIZER_PATH_ENV = "AGENTHUB_SUMMARY_DEEPSEEK_V41_TOKENIZER_PATH"
TOKENIZER_URL = (
    "https://modelscope.cn/models/deepseek-ai/DeepSeek-V4.1-Flash-1017/"
    f"resolve/{SUMMARY_NON_THINKING_COUNTER_REVISION}/tokenizer.json"
)
DEFAULT_TEST_TOKENIZER_PATH = (
    Path(__file__).resolve().parents[2]
    / ".scratch"
    / "model-assets"
    / "deepseek-v4.1-flash-1017"
    / SUMMARY_NON_THINKING_COUNTER_REVISION
    / "tokenizer.json"
)


def _verified_tokenizer_path(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"DeepSeek tokenizer asset is not a regular file: {resolved}")
    DeepseekV41TextCounter.from_file(resolved)
    return resolved


def _download_verified_tokenizer(destination: Path) -> Path:
    partial = destination.with_suffix(".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with httpx.stream(
            "GET",
            TOKENIZER_URL,
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
        ) as response:
            response.raise_for_status()
            with partial.open("wb") as output:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_TOKENIZER_BYTES:
                        raise ValueError("DeepSeek tokenizer asset exceeds the frozen size limit")
                    digest.update(chunk)
                    output.write(chunk)
        if digest.hexdigest() != SUMMARY_NON_THINKING_TOKENIZER_SHA256:
            raise ValueError("DeepSeek tokenizer asset failed the frozen digest check")
        partial.replace(destination)
        return _verified_tokenizer_path(destination)
    finally:
        partial.unlink(missing_ok=True)


def prepare_default_tokenizer() -> dict[str, str | int]:
    downloaded = False
    if DEFAULT_TEST_TOKENIZER_PATH.is_file():
        try:
            resolved = _verified_tokenizer_path(DEFAULT_TEST_TOKENIZER_PATH)
        except (OSError, TypeError, ValueError):
            resolved = None
    else:
        resolved = None
    if resolved is None:
        DEFAULT_TEST_TOKENIZER_PATH.parent.mkdir(parents=True, exist_ok=True)
        resolved = _download_verified_tokenizer(DEFAULT_TEST_TOKENIZER_PATH)
        downloaded = True
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return {
        "path": str(resolved),
        "bytes": resolved.stat().st_size,
        "sha256": digest,
        "source": TOKENIZER_URL,
        "downloaded": downloaded,
    }


@pytest.fixture(scope="session")
def deepseek_v41_tokenizer_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    configured = (os.environ.get(TOKENIZER_PATH_ENV) or "").strip()
    if configured:
        return _verified_tokenizer_path(Path(configured))
    if DEFAULT_TEST_TOKENIZER_PATH.is_file():
        return _verified_tokenizer_path(DEFAULT_TEST_TOKENIZER_PATH)
    return _download_verified_tokenizer(
        tmp_path_factory.mktemp("deepseek-v41-tokenizer") / "tokenizer.json"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare pinned public DeepSeek test assets.")
    parser.add_argument(
        "--prepare-default",
        action="store_true",
        help="Verify or download the pinned tokenizer at DEFAULT_TEST_TOKENIZER_PATH.",
    )
    args = parser.parse_args(argv)
    if not args.prepare_default:
        parser.error("--prepare-default is required")
    print(
        json.dumps(
            prepare_default_tokenizer(),
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_TEST_TOKENIZER_PATH",
    "TOKENIZER_PATH_ENV",
    "TOKENIZER_URL",
    "deepseek_v41_tokenizer_path",
    "prepare_default_tokenizer",
]
