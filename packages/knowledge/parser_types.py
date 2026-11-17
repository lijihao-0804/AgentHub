"""Provider-neutral output and safe errors from the isolated document parsers."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParsedBlock:
    text: str
    locator: dict[str, Any]


@dataclass(frozen=True)
class ParsedDocument:
    blocks: tuple[ParsedBlock, ...]


class DocumentParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# Short or empty cells still consume locator metadata and indexing work.
MAX_STRUCTURED_BLOCKS = 20_000
MAX_TABLE_CELLS = 65_536


def structure_limit() -> DocumentParseError:
    return DocumentParseError(
        "RESOURCE_LIMIT_EXCEEDED", "The document exceeds the supported table or block limits."
    )


def text_limit() -> DocumentParseError:
    return DocumentParseError(
        "RESOURCE_LIMIT_EXCEEDED", "The parsed document exceeds the configured character limit."
    )
