from __future__ import annotations

import json
import sys
from pathlib import Path

from packages.knowledge.parser import _parse_content, _ParserFailure


def main() -> None:
    input_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    media_type = sys.argv[3]
    max_pdf_pages = int(sys.argv[4])
    max_parsed_chars = int(sys.argv[5])
    try:
        parsed = _parse_content(
            input_path.read_bytes(),
            media_type,
            max_pdf_pages=max_pdf_pages,
            max_parsed_chars=max_parsed_chars,
        )
        result = {
            "ok": True,
            "blocks": [
                {"text": block.text, "locator": block.locator} for block in parsed.blocks
            ],
        }
    except _ParserFailure as exc:
        result = {"ok": False, "code": exc.code, "message": exc.message}
    except Exception:
        result = {
            "ok": False,
            "code": "PARSE_FAILED",
            "message": "The document could not be parsed.",
        }
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
