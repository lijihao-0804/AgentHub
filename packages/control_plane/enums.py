from enum import StrEnum


class OrganizationRole(StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"


class WorkspaceRole(StrEnum):
    DEVELOPER = "DEVELOPER"
    VIEWER = "VIEWER"
