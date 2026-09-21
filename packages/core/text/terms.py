"""Split a question into the strings worth matching on.

Deliberately not ``to_tsquery``. PostgreSQL ships no Chinese tokenizer, so a
Chinese question becomes one lexeme and full-text search degrades into
exact-sentence matching -- precisely the case the callers of this module exist
for. Latin text is split on word boundaries; CJK runs are cut into overlapping
bigrams, which is what every CJK search engine does in the absence of a
dictionary and what makes "上次的预算" find a sentence that said "预算是".

The corpora this serves are small by construction -- one thread's turns, one
agent's active memories -- so scanning them with case-insensitive containment
costs nothing and answers correctly in both languages, which an index-shaped
answer would not.
"""

from __future__ import annotations

import re

MAX_SEARCH_TERMS = 8
MIN_TERM_LENGTH = 2
_CJK = r"㐀-䶿一-鿿豈-﫿"
_LATIN_WORD = re.compile(r"[0-9A-Za-z_]+")
_CJK_RUN = re.compile(f"[{_CJK}]+")


def search_terms(query: str, *, limit: int = MAX_SEARCH_TERMS) -> list[str]:
    seen: list[str] = []

    def add(term: str) -> bool:
        if term and term not in seen:
            seen.append(term)
        return len(seen) < limit

    for run in _CJK_RUN.finditer(query):
        text_value = run.group()
        if len(text_value) == 1:
            if not add(text_value):
                return seen
            continue
        for index in range(len(text_value) - 1):
            if not add(text_value[index : index + 2]):
                return seen
    for word in _LATIN_WORD.finditer(query):
        term = word.group().lower()
        if len(term) < MIN_TERM_LENGTH:
            continue
        if not add(term):
            return seen
    return seen


__all__ = ["MAX_SEARCH_TERMS", "MIN_TERM_LENGTH", "search_terms"]
