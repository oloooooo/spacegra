from typing import Literal

from pydantic import BaseModel

Permission = Literal["read", "write"]


class MessagesBaseModel(BaseModel):
    message: str | None = None


class RegisterRequest(BaseModel):
    identity: str
    display_name: str
    org_id: str | None = None
    email: str | None = None
    password: str


class RegisterResponse(MessagesBaseModel):
    user_identity: str


class LoginRequest(BaseModel):
    # 登录不校验 identity 形态：历史账号可能含 `:`/`\` 这类字符，注册已经挡住了新的。
    identity: str
    password: str


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
