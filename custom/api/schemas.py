from typing import Literal

from pydantic import BaseModel, Field

Permission = Literal["read", "write"]


class MessagesBaseModel(BaseModel):
    message: str | None = None


class BootstrapRequest(BaseModel):
    """首次访问时创建超级用户（只有用户表为空时接受）。"""

    identity: str
    display_name: str
    password: str = Field(min_length=8)


class CreateUserRequest(BootstrapRequest):
    is_super: bool = False


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=8)


class UserOut(BaseModel):
    identity: str
    display_name: str
    is_super: bool


class LoginRequest(BaseModel):
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
