from __future__ import annotations

from typing import Any, Protocol


class TraceSink(Protocol):
    async def record(self, event_name: str, attributes: dict[str, Any]) -> None: ...
