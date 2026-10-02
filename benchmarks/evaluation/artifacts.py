"""Atomic reports with bounded retries for transient Windows file access errors."""

from __future__ import annotations

import errno
import json
import os
import time
from pathlib import Path
from uuid import uuid4


def write_json_atomic(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    for attempt in range(5):
        temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            return
        except OSError as error:
            transient = isinstance(error, PermissionError) or (
                os.name == "nt" and error.errno == errno.EINVAL
            )
            if not transient or attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1))
            # Retain failed temporary files for diagnostics; never truncate the old report.
