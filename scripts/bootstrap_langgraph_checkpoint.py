"""Compatibility entry point for the explicit LangGraph checkpoint bootstrap."""

from __future__ import annotations

import sys
from pathlib import Path

# Direct `python scripts/bootstrap_langgraph_checkpoint.py` runs arrive with
# no package context; put the repository root on sys.path first (the same
# guard scripts/bootstrap_checkpoint.py uses).
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.bootstrap_checkpoint import main

if __name__ == "__main__":
    main()
