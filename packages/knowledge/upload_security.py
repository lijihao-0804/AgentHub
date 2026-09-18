"""Streaming upload validation for the deliberately small M3 file baseline."""

from __future__ import annotations

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


ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md"}
_GENERIC_MEDIA_TYPES = {"", "application/octet-stream"}
_MEDIA_TYPES_BY_EXTENSION = {
    ".pdf": {"application/pdf", *_GENERIC_MEDIA_TYPES},
    ".txt": {"text/plain", *_GENERIC_MEDIA_TYPES},
    ".md": {"text/markdown", "text/plain", *_GENERIC_MEDIA_TYPES},
}


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
    if normalized not in _MEDIA_TYPES_BY_EXTENSION[extension]:
        raise UploadSecurityError("INVALID_FILE", "The uploaded media type is invalid.")
    if extension == ".pdf":
        return "application/pdf"
    if extension == ".md":
        return "text/markdown"
    return "text/plain"


def validate_content_signature(filename: str, prefix: bytes) -> None:
    extension = PurePath(filename).suffix.lower()
    if extension == ".pdf":
        valid = prefix.startswith(b"%PDF-")
    else:
        valid = b"\x00" not in prefix
        if valid:
            try:
                prefix.decode("utf-8")
            except UnicodeDecodeError:
                valid = False
    if not valid:
        raise UploadSecurityError("INVALID_FILE", "The uploaded file content is invalid.")


def enforce_stream_size(size_bytes: int, policy: UploadSecurityPolicy) -> None:
    if size_bytes > policy.max_file_size_bytes:
        raise UploadSecurityError("FILE_TOO_LARGE", "The uploaded file is too large.")
