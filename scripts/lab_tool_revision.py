"""Create a local-development ToolRevision for the runtime learning labs.

This is a privileged direct-database helper, not an HTTP/API operation. It is intended
only for an isolated local AgentHub database; the supplied identity is recorded for
audit but is not authenticated or checked against workspace membership by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID

from sqlalchemy import select

from packages.agent_runtime.models import ToolRevision
from packages.agent_runtime.tool_revisions import ToolRevisionService
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True, type=UUID)
    parser.add_argument("--organization-id", required=True, type=UUID)
    parser.add_argument("--user-id", required=True, type=UUID)
    parser.add_argument("--tool-id", required=True, type=UUID)
    parser.add_argument("field", choices=("effect", "approval_policy"))
    parser.add_argument("value", choices=("READ", "WRITE", "NEVER", "ALWAYS"))
    return parser


async def _create_revision(args: argparse.Namespace) -> None:
    allowed_values = {
        "effect": {"READ", "WRITE"},
        "approval_policy": {"NEVER", "ALWAYS"},
    }
    if args.value not in allowed_values[args.field]:
        raise SystemExit(f"{args.value} is not valid for {args.field}")

    context = WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="runtime-lab-tool-revision",
                trace_id="runtime-lab-tool-revision",
                user_id=str(args.user_id),
            ),
            organization_id=str(args.organization_id),
            org_role="MEMBER",
        ),
        workspace_id=str(args.workspace_id),
        workspace_role="DEVELOPER",
        permissions=frozenset({"tool_edit"}),
    )
    engine, session_factory = create_database(get_settings().database_url)
    try:
        async with session_factory() as session:
            latest = await session.scalar(
                select(ToolRevision)
                .where(
                    ToolRevision.workspace_id == args.workspace_id,
                    ToolRevision.tool_id == args.tool_id,
                )
                .order_by(ToolRevision.revision_number.desc())
                .limit(1)
            )
            if latest is None:
                raise SystemExit("No ToolRevision exists for this tool in the workspace")
            spec = dict(latest.spec)
            previous = spec.get(args.field)
            spec[args.field] = args.value
            revision = await ToolRevisionService().create_revision(
                session, context, tool_id=args.tool_id, spec=spec
            )
            print(
                json.dumps(
                    {
                        "previous_revision": latest.revision_number,
                        "new_revision": revision.revision_number,
                        "field": args.field,
                        "previous_value": previous,
                        "new_value": args.value,
                        "spec_hash": revision.spec_hash,
                        "next_step": (
                            "Publish the lab Agent so its resolved_spec pins this revision."
                        ),
                    },
                    indent=2,
                )
            )
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(_create_revision(_parser().parse_args()))


if __name__ == "__main__":
    main()
