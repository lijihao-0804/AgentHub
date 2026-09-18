"""Provider-neutral blob storage contract and local filesystem adapter."""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Protocol


class BlobStoreError(ValueError):
    """Raised when a blob key or local storage operation is unsafe."""


@dataclass(frozen=True)
class StoredBlob:
    blob_key: str
    size_bytes: int


class BlobStore(Protocol):
    async def put(self, blob_key: str, chunks: AsyncIterable[bytes]) -> StoredBlob: ...

    def read(self, blob_key: str) -> AsyncIterator[bytes]: ...

    async def delete(self, blob_key: str) -> None: ...


class LocalBlobStore:
    """Atomic, root-confined local blob storage for API and worker sharing."""

    def __init__(self, root: str | Path, *, read_chunk_size: int = 1024 * 1024) -> None:
        self.root = Path(root).expanduser().resolve()
        self.read_chunk_size = read_chunk_size

    def _path_for(self, blob_key: str) -> Path:
        if not blob_key or "\x00" in blob_key:
            raise BlobStoreError("BLOB_KEY_INVALID")
        posix_key = PurePosixPath(blob_key)
        windows_key = PureWindowsPath(blob_key)
        if (
            posix_key.is_absolute()
            or windows_key.is_absolute()
            or bool(windows_key.drive)
            or any(part in {"", ".", ".."} for part in posix_key.parts)
            or "\\" in blob_key
        ):
            raise BlobStoreError("BLOB_KEY_INVALID")

        candidate = (self.root / Path(*posix_key.parts)).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise BlobStoreError("BLOB_KEY_INVALID") from exc
        if candidate.exists() and candidate.is_symlink():
            raise BlobStoreError("BLOB_KEY_INVALID")
        return candidate

    async def put(self, blob_key: str, chunks: AsyncIterable[bytes]) -> StoredBlob:
        destination = self._path_for(blob_key)
        self.root.mkdir(parents=True, exist_ok=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        size_bytes = 0
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.root, prefix=".upload-", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                async for chunk in chunks:
                    if not chunk:
                        continue
                    temporary.write(chunk)
                    size_bytes += len(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
            temporary_path = None
            return StoredBlob(blob_key=blob_key, size_bytes=size_bytes)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    async def delete(self, blob_key: str) -> None:
        path = self._path_for(blob_key)
        if path.exists():
            path.unlink()

    async def _read(self, blob_key: str) -> AsyncIterator[bytes]:
        path = self._path_for(blob_key)
        try:
            with path.open("rb") as source:
                while chunk := source.read(self.read_chunk_size):
                    yield chunk
        except FileNotFoundError as exc:
            raise BlobStoreError("BLOB_NOT_FOUND") from exc

    def read(self, blob_key: str) -> AsyncIterator[bytes]:
        return self._read(blob_key)
