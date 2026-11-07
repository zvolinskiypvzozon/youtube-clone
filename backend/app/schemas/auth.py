import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from app.models import UserRole

Username = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9_]{3,30}$"),
    Field(description="3–30 символов: латиница, цифры, _", examples=["alice_42"]),
]
Password = Annotated[
    str, Field(min_length=8, max_length=128, description="8–128 символов", examples=["s3cret-pass"])
]


class RegisterIn(BaseModel):
    email: EmailStr = Field(max_length=254, examples=["alice@example.com"])
    username: Username
    password: Password


class LoginIn(BaseModel):
    login: str = Field(
        min_length=3,
        max_length=254,
        description="Email или имя пользователя",
        examples=["alice_42"],
    )
    password: str = Field(min_length=1, max_length=128)


class ChannelBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    handle: str
    title: str


class MeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    username: str
    role: UserRole
    created_at: datetime
    channel: ChannelBrief


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 — тип токена по RFC 6750, не секрет
