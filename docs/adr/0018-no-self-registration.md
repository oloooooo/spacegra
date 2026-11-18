# 不做自助注册：首个账号即超级用户，之后由超管开号

2026-11-18 起，`POST /custom/register` 删除。改成：**只有用户表为空时（第一次访问）能建出
第一个账号**，它自动是超级用户；之后所有账号由超级用户在界面里创建、重置密码、删除。
参考 yuxi 0.8.0 的 `POST /auth/initialize` + `check-first-run` + 用户管理，但**不做**
OAuth / OIDC / CLI device code。

## 为什么

| 旧：自助注册 | 问题 |
|---|---|
| 任何能访问服务的人都能 `POST /custom/register` | 服务通常直接暴露在内网/VPN，等于谁都能拿到 `read` 权限：能建会话、跑图、烧模型额度 |
| 权限只按 `user_tables.is_super` 分，而 `is_super` 只能直接改库 | 注册出来的人永远平级，没有"管理员"这个角色能落地 |
| 没有禁用/删除入口 | 账号只增不减，离职的人 token 到期前一直能用（一天可重登一次） |

"第一次访问建超管"是 yuxi 已验证的形态：它把"系统是否已初始化"当成一个显式状态，
初装时由第一个访问者完成，之后这条路径永久关闭。

## 机制

### 1. 首访可建超管（`custom/api/account.py`）

```http
GET  /custom/first-run   -> {"first_run": count(user_tables) == 0}     # 不鉴权，登录页要用
POST /custom/bootstrap   -> 建 is_super=True 的账号并直接返回 access token
                          用户表非空时 403 {"detail": "系统已初始化，请直接登录"}
```

"数用户 → 插入"必须原子，否则两个并发首访各数到 0、各建一个超管：

```python
await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BOOTSTRAP_LOCK_KEY})
if await repo.count() > 0:
    raise HTTPException(403, "系统已初始化，请直接登录")
```

锁是**事务级**的（`pg_advisory_xact_lock`），commit/rollback 自动释放，不用手工解锁；
锁键是常数（`0x41454752`），同库内所有副本共用一个。这要求 Postgres（本项目本来就只有 PG）。

### 2. 超管开号（`custom/api/account.py`）

```http
GET    /custom/users                      # 列表
POST   /custom/users                      # {identity, display_name, password, is_super}
POST   /custom/users/{identity}/password  # 重置密码（没有找回密码流程）
DELETE /custom/users/{identity}           # 不能删自己
```

- 全部要求 `permissions` 含 `write`（`require_super`），普通用户 403。
- 分配空间仍走既有的 `POST /custom/spaces/{name}/users`（ADR-0005 的 space 模型不变）。
- 账号名规则：`^[A-Za-z0-9_]{3,20}$`；保留名 `subagent_service`（服务身份在 Auth handler
  里免查库放行，占用了就能绕过鉴权）与 `local`（run config 缺身份时的兜底）。
- 显示名 ≤10 字（`user_tables.display_name` 是 `String(10)`）；密码 8-72 字节
  （bcrypt 5.x 对 >72 字节直接抛 `ValueError`，登录侧也 catch 成"密码错误"而不是 500）。

## 已知边界

- **没有找回密码**：忘了只能另一个超管重置；只剩一个超管又忘了密码，就得直接改库。
- **删账号不清理该用户的 thread/checkpoint**：`thread` 表没有指向 `user_tables` 的 FK
  （原生表），行还在、checkpoint 还在，只是没人能再看到（按属主过滤）。要清理得手工。
- **没有登录限速**：密码错误只返回统一 401，不锁账号。真要防爆破得在反向代理层做。
- **`first_run` 泄露"系统是否已初始化"**：登录页必须知道这件事，接受。

## Considered Options

- **保留注册 + 邀请码**：多一张表、多一套码的签发/过期，收益只是"注册时少一次管理员操作"。
- **保留注册 + 首个注册者自动超管**：和 bootstrap 等价，但把"抢注"窗口留着——服务刚起来
  时谁先访问谁当管理员。bootstrap 至少语义明确（只在用户表为空时接受）。
- **接入 OAuth/OIDC**：用户明确不要；自建账号 + JWT 已经够这套部署用。
- **bootstrap（选中）**：无新表、无新配置，一条 403 就把窗口永久关掉。

## Consequences

- 前端 `web/src/components/LoginView.vue` 先查 `GET /custom/first-run`：`true` 显示"初始化超级用户"
  表单，`false` 显示登录表单；初始化成功直接进主界面（后端返回 token）。
- 超管界面在 `SidePanel.vue` 的「用户」tab（普通用户看不到这个 tab）。
- 首次部署顺序变成：起服务 → 打开页面 → 建第一个账号。
