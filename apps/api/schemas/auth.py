from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CredentialsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized):
            raise ValueError("email must be valid")
        return value


class AuthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class LogoutResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "logged_out"
