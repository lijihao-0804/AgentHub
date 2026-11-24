"""Progressive-disclosure Agent Skills and KV-safe hierarchical tool discovery.

Implements two complementary mechanisms from Chapter 2 & Chapter 4:
1. Three-layer Progressive Disclosure Skills:
   - Layer 1: compact catalog (name + routing description) in static prefix.
   - Layer 2: full skill instructions loaded on demand via ``load_skill`` and
     appended to the trajectory (preserving prior KV-cache blocks).
   - Layer 3: detailed reference docs loaded on demand by name.
2. Two-stage Hierarchical Tool Discovery (``discover_tools``):
   - Matches deferred MCP/workspace tools by server/category and lexical
     relevance without bloating or mutating the frozen top ``tools`` prefix.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from packages.knowledge.contracts import DenseEmbedder
from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolRisk,
    ToolSourceKind,
)

LOAD_SKILL_TOOL = "load_skill"
DISCOVER_TOOLS_TOOL = "discover_tools"
_LOAD_SKILL_REV_ID = UUID("00000000-0000-0000-0000-00000000a003")
_DISCOVER_TOOLS_REV_ID = UUID("00000000-0000-0000-0000-00000000a004")
_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]")


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


@dataclass(frozen=True, slots=True)
class SkillEntry:
    name: str
    description: str
    instructions: str
    references: Mapping[str, str] = field(default_factory=dict)


class SkillCatalog:
    """In-memory progressive-disclosure registry of versioned Skills."""

    def __init__(self, skills: Sequence[SkillEntry]) -> None:
        self._by_name: dict[str, SkillEntry] = {skill.name: skill for skill in skills}

    def render_catalog(self) -> str:
        """Layer 1: deterministic compact metadata catalog for static prefix."""
        if not self._by_name:
            return ""
        lines = ["<available_skills>"]
        for name in sorted(self._by_name):
            skill = self._by_name[name]
            lines.append(f"- {skill.name}: {skill.description}")
        lines.append("</available_skills>")
        return "\n".join(lines)

    def load_skill(
        self,
        *,
        name: str,
        reference: str | None = None,
    ) -> dict[str, Any]:
        """Layer 2 & Layer 3: load instructions or a specific reference on demand."""
        skill = self._by_name.get(name.strip())
        if skill is None:
            return {
                "error_code": "SKILL_NOT_FOUND",
                "message": f"Skill '{name}' was not found.",
                "available_skills": sorted(self._by_name),
            }
        if reference is not None:
            ref_key = reference.strip()
            ref_body = skill.references.get(ref_key)
            if ref_body is None:
                return {
                    "error_code": "SKILL_REFERENCE_NOT_FOUND",
                    "message": f"Reference '{ref_key}' not found in skill '{skill.name}'.",
                    "available_references": sorted(skill.references),
                }
            return {
                "skill": skill.name,
                "layer": 3,
                "reference": ref_key,
                "content": ref_body,
            }
        return {
            "skill": skill.name,
            "layer": 2,
            "instructions": skill.instructions,
            "available_references": sorted(skill.references),
        }


@dataclass(frozen=True, slots=True)
class DeferredToolEntry:
    name: str
    server: str
    description: str
    parameters: Mapping[str, Any]
    tags: tuple[str, ...] = ()


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return max(0.0, dot / (na * nb))


class ToolDiscoveryRegistry:
    """Two-stage server -> tool hybrid (dense + lexical) discovery over deferred tools."""

    def __init__(
        self,
        entries: Sequence[DeferredToolEntry],
        *,
        embedder: DenseEmbedder | None = None,
    ) -> None:
        self._entries = tuple(sorted(entries, key=lambda e: (e.server, e.name)))
        self._embedder = embedder
        self._entry_vectors: dict[tuple[str, str], tuple[float, ...]] = {}
        if embedder is not None and self._entries:
            texts = [
                f"{e.server} {e.name}: {e.description} {' '.join(e.tags)}"
                for e in self._entries
            ]
            vectors = embedder.embed_documents(texts)
            for entry, vec in zip(self._entries, vectors, strict=True):
                self._entry_vectors[(entry.server, entry.name)] = tuple(vec)

    def discover(
        self,
        *,
        query: str,
        server: str | None = None,
        limit: int = 5,
        min_score: float = 0.15,
    ) -> dict[str, Any]:
        """Match deferred tools and return schemas for trajectory-tail append."""
        q_tokens = _tokenize(query)
        if not q_tokens:
            return {
                "matched": [],
                "hint": "Provide descriptive keywords for the capability you need.",
            }
        bounded_limit = max(1, min(int(limit), 10))
        server_filter = (
            server.strip().lower() if isinstance(server, str) and server.strip() else None
        )
        query_vec = self._embedder.embed_query(query) if self._embedder is not None else None

        scored: list[tuple[float, DeferredToolEntry]] = []
        for entry in self._entries:
            if server_filter is not None and server_filter not in entry.server.lower():
                continue
            name_tokens = _tokenize(entry.name)
            server_tokens = _tokenize(entry.server)
            desc_tokens = _tokenize(entry.description)
            tag_tokens = _tokenize(" ".join(entry.tags))
            all_tokens = name_tokens | server_tokens | desc_tokens | tag_tokens
            lexical = 0.0
            if all_tokens:
                overlap = len(q_tokens & all_tokens) / len(q_tokens)
                name_boost = 0.25 if (q_tokens & name_tokens) else 0.0
                server_boost = 0.20 if (q_tokens & server_tokens) else 0.0
                lexical = min(1.0, overlap + name_boost + server_boost)
            if query_vec is not None and (entry.server, entry.name) in self._entry_vectors:
                dense = _cosine_similarity(
                    query_vec, self._entry_vectors[(entry.server, entry.name)]
                )
                score = round(0.65 * dense + 0.35 * lexical, 4)
            else:
                score = round(lexical, 4)
            if score >= min_score:
                scored.append((score, entry))

        scored.sort(key=lambda pair: (-pair[0], pair[1].server, pair[1].name))
        top = scored[:bounded_limit]
        if not top:
            return {
                "matched": [],
                "available_servers": sorted({e.server for e in self._entries}),
                "hint": (
                    "No deferred tool met the relevance threshold. "
                    "Rephrase your capability need or specify one of available_servers."
                ),
            }
        return {
            "matched": [
                {
                    "name": entry.name,
                    "server": entry.server,
                    "score": score,
                    "description": entry.description,
                    "parameters": dict(entry.parameters),
                }
                for score, entry in top
            ],
            "total_catalog_tools": len(self._entries),
        }


def load_skill_definition() -> ToolDefinition:
    return ToolDefinition(
        identity=LOAD_SKILL_TOOL,
        revision_id=_LOAD_SKILL_REV_ID,
        spec_hash="builtin:load_skill:v1",
        description=(
            "Load full instructions (layer 2) or a named reference (layer 3) "
            "for a skill listed in <available_skills>."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "reference": {"type": "string"},
            },
        },
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=5,
        source_kind=ToolSourceKind.BUILTIN,
    )


def discover_tools_definition() -> ToolDefinition:
    return ToolDefinition(
        identity=DISCOVER_TOOLS_TOOL,
        revision_id=_DISCOVER_TOOLS_REV_ID,
        spec_hash="builtin:discover_tools:v1",
        description=(
            "Search deferred workspace/MCP tools by capability description and "
            "optional server name. Matched schemas are appended to the trajectory."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "server": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
        },
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=5,
        source_kind=ToolSourceKind.BUILTIN,
    )


__all__ = [
    "DISCOVER_TOOLS_TOOL",
    "DeferredToolEntry",
    "LOAD_SKILL_TOOL",
    "SkillCatalog",
    "SkillEntry",
    "ToolDiscoveryRegistry",
    "discover_tools_definition",
    "load_skill_definition",
]
