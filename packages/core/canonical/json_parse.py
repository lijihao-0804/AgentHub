"""Parse model-supplied JSON without letting a later value replace an earlier field."""

import json
from typing import Any


def load_unique_json(text: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate JSON field")
        return result

    def reject_constant(_value: str):
        raise ValueError("non-finite JSON constant")

    return json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
