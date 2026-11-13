"""Provision the disposable frontend QA account with least-privilege membership.

The account is created through the public auth API. Membership changes then go
through TenantService, the same domain service used by the product API, using an
existing OWNER/ADMIN only as the audited actor. The script never changes an
existing account or business row and is idempotent for the disposable account.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from sqlalchemy import select

from packages.control_plane.enums import OrganizationRole, WorkspaceRole
from packages.control_plane.models import (
    Organization,
    OrganizationMembership,
    Workspace,
    WorkspaceMembership,
)
from packages.control_plane.services import TenantService
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.core.execution_context.models import PrincipalContext


def post_json(api_base_url: str, path: str, payload: dict[str, str]) -> tuple[int, dict]:
    request = Request(
        f"{api_base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"error": {"code": "HTTP_ERROR"}}
        return error.code, parsed


def write_account(path: Path, account: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(account, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


async def provision(args: argparse.Namespace) -> dict[str, str]:
    credential_path = Path(args.credential_file).resolve()
    if credential_path.exists():
        account = json.loads(credential_path.read_text(encoding="utf-8"))
        email = str(account["email"])
        password = str(account["password"])
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        email = f"frontend-qa-{stamp}-{secrets.token_hex(4)}@example.test"
        password = secrets.token_urlsafe(30)

    status, auth_body = post_json(
        args.api_base_url,
        "/api/v1/auth/register",
        {"email": email, "password": password},
    )
    if status == 201:
        user_id = UUID(str(auth_body["user_id"]))
    else:
        code = auth_body.get("error", {}).get("code")
        if code != "EMAIL_ALREADY_REGISTERED":
            raise RuntimeError(f"QA registration failed with HTTP {status}: {code}")
        status, auth_body = post_json(
            args.api_base_url,
            "/api/v1/auth/login",
            {"email": email, "password": password},
        )
        if status != 200:
            raise RuntimeError(
                f"Existing QA account login failed with HTTP {status}: "
                f"{auth_body.get('error', {}).get('code')}"
            )
        user_id = UUID(str(auth_body["user_id"]))

    settings = get_settings()
    engine, session_factory = create_database(settings.database_url)
    try:
        async with session_factory() as session:
            organization = await session.scalar(
                select(Organization).where(Organization.name == args.organization)
            )
            if organization is None:
                raise RuntimeError(f"Organization not found: {args.organization}")
            workspace = await session.scalar(
                select(Workspace).where(
                    Workspace.organization_id == organization.id,
                    Workspace.name == args.workspace,
                )
            )
            if workspace is None:
                raise RuntimeError(f"Workspace not found: {args.workspace}")
            actor_user_id = await session.scalar(
                select(OrganizationMembership.user_id)
                .where(
                    OrganizationMembership.organization_id == organization.id,
                    OrganizationMembership.role.in_(
                        (OrganizationRole.OWNER, OrganizationRole.ADMIN)
                    ),
                )
                .order_by(OrganizationMembership.user_id)
                .limit(1)
            )
            if actor_user_id is None:
                raise RuntimeError("The target organization has no OWNER or ADMIN actor")

            request_id = f"frontend-product-qa-{uuid4()}"
            principal = PrincipalContext(
                request_id=request_id,
                trace_id=request_id,
                user_id=str(actor_user_id),
            )
            membership = await session.get(
                OrganizationMembership,
                (organization.id, user_id),
            )
            org_membership_created = membership is None
            if membership is None:
                await TenantService().add_organization_member(
                    session,
                    principal=principal,
                    organization_id=organization.id,
                    user_id=user_id,
                    role=OrganizationRole.MEMBER,
                )
            elif membership.role != OrganizationRole.MEMBER:
                raise RuntimeError(
                    "QA account already has a non-minimal organization role; refusing to change it"
                )

            workspace_membership = await session.get(
                WorkspaceMembership,
                (workspace.id, user_id),
            )
            workspace_membership_created = workspace_membership is None
            if workspace_membership is None:
                await TenantService().add_workspace_member(
                    session,
                    principal=principal,
                    workspace_id=workspace.id,
                    user_id=user_id,
                    role=WorkspaceRole.VIEWER,
                )
            elif workspace_membership.role != WorkspaceRole.VIEWER:
                raise RuntimeError(
                    "QA account already has a non-minimal workspace role; refusing to change it"
                )
    finally:
        await engine.dispose()

    account = {
        "email": email,
        "password": password,
        "user_id": str(user_id),
        "organization_id": str(organization.id),
        "workspace_id": str(workspace.id),
        "organization_name": organization.name,
        "workspace_name": workspace.name,
        "organization_role": OrganizationRole.MEMBER.value,
        "workspace_role": WorkspaceRole.VIEWER.value,
        "created_memberships": {
            "organization": org_membership_created,
            "workspace": workspace_membership_created,
        },
    }
    write_account(credential_path, account)
    return account


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--organization", default="Verification Org")
    parser.add_argument("--workspace", default="Research")
    parser.add_argument(
        "--credential-file",
        default=str(Path.home() / ".codex" / "frontend-product-qa-account.json"),
    )
    args = parser.parse_args()
    account = asyncio.run(provision(args))
    safe = {key: value for key, value in account.items() if key != "password"}
    print(json.dumps(safe, ensure_ascii=False))


if __name__ == "__main__":
    main()
