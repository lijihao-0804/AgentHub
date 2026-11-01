"""Conservative cache capability information for known deployment routes.

This does not configure provider caching or constitute a cache namespace.
Implicit provider caches are outside AgentHub's control and must not be
confused with local summary reuse or final-answer caching.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit


class CacheSupport(StrEnum):
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"
    SUPPORTED = "SUPPORTED"
    ENABLED = "ENABLED"


@dataclass(frozen=True, slots=True)
class PromptCacheCapability:
    implicit: CacheSupport
    explicit: CacheSupport
    evidence: str | None = None


def prompt_cache_capability(*, base_url: str | None, model: str) -> PromptCacheCapability:
    """Match only the documented, exact deployment; proxy routes stay unknown.

    SUPPORTED records documentation, not a verified hit or a per-account
    deployment guarantee. Explicit markers are not emitted by this adapter.
    """
    if isinstance(base_url, str) and isinstance(model, str):
        try:
            parts = urlsplit(base_url)
            direct_beijing = (
                parts.scheme == "https"
                and parts.netloc.lower() == "dashscope.aliyuncs.com"
                and parts.path.rstrip("/") == "/compatible-mode/v1"
                and not parts.query
                and not parts.fragment
            )
        except ValueError:
            direct_beijing = False
        if direct_beijing and model == "deepseek-v4.1-flash-1017":
            return PromptCacheCapability(
                implicit=CacheSupport.SUPPORTED,
                explicit=CacheSupport.UNKNOWN,
                evidence="https://help.aliyun.com/zh/model-studio/deepseek-api",
            )
    return PromptCacheCapability(CacheSupport.UNKNOWN, CacheSupport.UNKNOWN)
