"""Streaming upload validation for knowledge documents and parser result imports."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from pathlib import PurePath, PureWindowsPath


class UploadSecurityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class UploadSecurityPolicy:
    max_file_size_bytes: int = 10 * 1024 * 1024


_GENERIC_MEDIA_TYPES = {"", "application/octet-stream"}
# First value is the canonical media type persisted on the revision and dispatched
# by the worker. Browsers use different MIME aliases for delimited text.
_MEDIA_TYPES_BY_EXTENSION = {
    ".pdf": ("application/pdf",),
    ".txt": ("text/plain",),
    ".md": ("text/markdown", "text/plain"),
    ".csv": ("text/csv", "application/vnd.ms-excel", "text/plain", "application/csv"),
    ".tsv": ("text/tab-separated-values", "text/plain"),
    ".json": ("application/json",),
}
ALLOWED_EXTENSIONS = frozenset(_MEDIA_TYPES_BY_EXTENSION)


def validate_original_filename(filename: str | None) -> str:
    if not filename or "\x00" in filename:
        raise UploadSecurityError("INVALID_FILE", "The uploaded filename is invalid.")
    if filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise UploadSecurityError("PATH_TRAVERSAL", "The uploaded filename is invalid.")
    if (
        PurePath(filename).is_absolute()
        or PureWindowsPath(filename).is_absolute()
        or bool(PureWindowsPath(filename).drive)
    ):
        raise UploadSecurityError("PATH_TRAVERSAL", "The uploaded filename is invalid.")
    extension = PurePath(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise UploadSecurityError("UNSUPPORTED_FILE", "The uploaded file type is not supported.")
    return filename


def validate_declared_media_type(filename: str, media_type: str | None) -> str:
    extension = PurePath(filename).suffix.lower()
    normalized = (media_type or "").split(";", 1)[0].strip().lower()
    allowed = _MEDIA_TYPES_BY_EXTENSION[extension]
    if normalized not in allowed and normalized not in _GENERIC_MEDIA_TYPES:
        raise UploadSecurityError("INVALID_FILE", "The uploaded media type is invalid.")
    return allowed[0]


def validate_content_signature(filename: str, prefix: bytes) -> None:
    extension = PurePath(filename).suffix.lower()
    if extension == ".pdf":
        valid = prefix.startswith(b"%PDF-")
    else:
        valid = b"\x00" not in prefix
        if valid:
            try:
                # The route validates only a bounded prefix. A complete file may
                # end in the middle of a multi-byte code point at that boundary,
                # so leave a trailing partial sequence buffered rather than
                # treating it as malformed UTF-8.
                decoded = codecs.getincrementaldecoder("utf-8-sig")().decode(prefix, final=False)
                if extension == ".json":
                    first = decoded.lstrip(" \r\n\t")[:1]
                    valid = not first or first in {"[", "{"}
            except UnicodeDecodeError:
                valid = False
    if not valid:
        raise UploadSecurityError("INVALID_FILE", "The uploaded file content is invalid.")


def enforce_stream_size(size_bytes: int, policy: UploadSecurityPolicy) -> None:
    if size_bytes > policy.max_file_size_bytes:
        raise UploadSecurityError("FILE_TOO_LARGE", "The uploaded file is too large.")
