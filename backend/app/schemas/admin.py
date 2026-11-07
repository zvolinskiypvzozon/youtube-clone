import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import UserRole


class AdminUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    username: str
    role: UserRole
    is_banned: bool
    created_at: datetime


class AdminUserPage(BaseModel):
    items: list[AdminUserOut]
    total: int
    limit: int
    offset: int


class RoleUpdateIn(BaseModel):
    role: UserRole
