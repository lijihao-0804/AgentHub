"""Unit tests for progressive-disclosure Skills and KV-safe tool discovery."""

from __future__ import annotations

from packages.agent_runtime.skills_and_discovery import (
    DISCOVER_TOOLS_TOOL,
    LOAD_SKILL_TOOL,
    DeferredToolEntry,
    SkillCatalog,
    SkillEntry,
    ToolDiscoveryRegistry,
    discover_tools_definition,
    load_skill_definition,
)


def test_skill_catalog_three_layer_progressive_disclosure() -> None:
    catalog = SkillCatalog(
        [
            SkillEntry(
                name="refund_policy",
                description=(
                    "Use when processing customer refund or SLA disputes; skip for tech bugs."
                ),
                instructions="1. Check order date.\n2. Verify invoice.\n3. Propose ticket.",
                references={
                    "special_fares.md": "Promotional tickets are non-refundable after 24h."
                },
            ),
            SkillEntry(
                name="sql_analysis",
                description="Use when comparing KPI metrics across UTC date windows.",
                instructions="Always inspect table schema before running SQL.",
            ),
        ]
    )
    # Layer 1: compact catalog only, no full instructions leaked
    layer1 = catalog.render_catalog()
    assert "<available_skills>" in layer1
    assert "refund_policy:" in layer1
    assert "Verify invoice" not in layer1

    # Layer 2: full instructions + reference index
    layer2 = catalog.load_skill(name="refund_policy")
    assert layer2["layer"] == 2
    assert "Verify invoice" in layer2["instructions"]
    assert layer2["available_references"] == ["special_fares.md"]

    # Layer 3: specific reference loaded on demand
    layer3 = catalog.load_skill(name="refund_policy", reference="special_fares.md")
    assert layer3["layer"] == 3
    assert "non-refundable" in layer3["content"]

    # Missing skill returns actionable available list
    missing = catalog.load_skill(name="unknown_skill")
    assert missing["error_code"] == "SKILL_NOT_FOUND"
    assert "refund_policy" in missing["available_skills"]


def test_tool_discovery_registry_hierarchical_matching_and_fallback() -> None:
    registry = ToolDiscoveryRegistry(
        [
            DeferredToolEntry(
                name="github_list_prs",
                server="github",
                description="List pull requests and review status in a GitHub repository.",
                parameters={"type": "object", "properties": {"repo": {"type": "string"}}},
                tags=("git", "pr", "code"),
            ),
            DeferredToolEntry(
                name="jira_create_issue",
                server="jira",
                description="Create a Jira ticket in a project board.",
                parameters={"type": "object", "properties": {"project": {"type": "string"}}},
                tags=("ticket", "issue"),
            ),
            DeferredToolEntry(
                name="warehouse_run_sql",
                server="warehouse",
                description="Execute read-only SQL analytics query.",
                parameters={"type": "object", "properties": {"sql": {"type": "string"}}},
                tags=("analytics", "sql"),
            ),
        ]
    )

    hit = registry.discover(query="github pull requests repo")
    assert hit["matched"][0]["name"] == "github_list_prs"
    assert hit["total_catalog_tools"] == 3

    by_server = registry.discover(query="ticket issue", server="jira")
    assert [m["name"] for m in by_server["matched"]] == ["jira_create_issue"]

    miss = registry.discover(query="quantum teleportation")
    assert miss["matched"] == []
    assert "available_servers" in miss

    assert load_skill_definition().identity == LOAD_SKILL_TOOL
    assert discover_tools_definition().identity == DISCOVER_TOOLS_TOOL
