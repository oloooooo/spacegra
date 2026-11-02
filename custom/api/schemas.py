from typing import Literal

from pydantic import BaseModel, field_validator

from shared.paths import safe_segment

Permission = Literal["read", "write"]

_IDENTITY_MAX = 64


def _identity(value: str) -> str:
    """identity 会拼进上传目录（``<owner>/<thread>/<名>``），必须是单个路径段。"""
    try:
        return safe_segment(value, limit=_IDENTITY_MAX)
    except ValueError as e:
        raise ValueError(f"账号名不合法：{e}") from e


class MessagesBaseModel(BaseModel):
    message: str | None = None


class RegisterRequest(BaseModel):
    identity: str
    display_name: str
    org_id: str | None = None
    email: str | None = None
    password: str

    _check_identity = field_validator("identity")(_identity)


class RegisterResponse(MessagesBaseModel):
    user_identity: str


class LoginRequest(BaseModel):
    identity: str
    password: str

    _check_identity = field_validator("identity")(_identity)


class TokenResponse(MessagesBaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class ListUserSpaceResponse(MessagesBaseModel):
    space: list[str]
    message: str | None = None


class CreateSpaceInput(BaseModel):
    space_name: str
    # 已废弃：目录由 space_name 派生（见 shared/knowledge/spec.py），此字段仅为老客户端兼容
    path: str = ""


class CreateSpaceResponse(MessagesBaseModel):
    space_name: str | None


class CreateUserSpaceResponse(CreateSpaceResponse):
    user_identity: str
