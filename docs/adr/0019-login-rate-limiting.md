# 登录限流：账号级锁定 + IP 滑动窗口

2026-11-18 起，`POST /custom/login` 有两层限流：**账号连续 5 次失败锁 300 秒**（`403`），
**同一 IP 的失败次数进 Redis 滑动窗口**（`IP+账号` 10 次、`IP` 30 次，窗口 600 秒，`429`）。
规格照抄 yuxi 0.8.0 的 `login_limits.py` + `UserTables` 锁定字段。
见 [ADR-0018](./0018-no-self-registration.md)：取消自助注册之后，密码登录成了唯一入口，
爆破面就全压在它身上。

## 为什么两层

| 只有账号锁定 | 问题 |
|---|---|
| 攻击者换着账号名试 | 每个账号只失败 1 次，永远撞不到阈值——这就是"密码喷洒" |
| 一个 IP 扫一片弱口令 | 同上，账号维度看不见 |

| 只有 IP 限流 | 问题 |
|---|---|
| 攻击者换 IP（代理池）盯一个账号 | IP 维度看不见；账号锁定能挡住 |

所以：**账号维度的状态落库**（跨副本、跨重启共享），**IP 维度的计数落 Redis**（滑动窗口，
跨副本共享）。两道阈值故意不同（5 vs 10）：正常用户先撞到"账号被锁定 N 秒"这个明确提示，
而不是含混的"太频繁"。

## 机制

### 1. 账号级锁定（`custom/db/models.py`，`user_tables` 三列）

```
login_failed_count  int   not null default 0     连续失败次数
last_failed_login   timestamptz                  最后一次失败时间
login_locked_until  timestamptz                  锁定到期时间
```

- 常量：`MAX_LOGIN_FAILED_ATTEMPTS = 5`、`LOGIN_LOCK_DURATION_SECONDS = 300`；
  状态机是 `UserTables` 上的方法（`increment_failed_login` / `is_login_locked` /
  `lock_remaining_seconds` / `reset_failed_login`），路由只负责编排。
- 达到阈值时把 `login_locked_until` 设成"这次失败 + 300s"，锁定期间**不验密码**——
  否则锁定只是给爆破加了个延迟，攻击者照样能从响应里读出密码对不对。
- 锁定**过期后先清零再验密码**：否则解锁后的第一次失误会立刻重新锁上。
- 成功登录清零。只在计数非 0 时写库，正常登录不多一次 UPDATE。
- 账号不存在也记一笔失败，且返回与密码错误完全相同的 `401 账号或密码错误`：
  错误文案和错误计数都不能成为账号枚举的探针。

### 2. IP 滑动窗口（`custom/service/login_limits.py`）

两个 Redis zset（score = 失败时间戳）：

```
aegra:login-failure:ip:<ip>                     窗口内该 IP 的全部失败
aegra:login-failure:ipacct:sha1(<ip>|<账号>)    窗口内该 IP + 该账号的失败
```

拦截时 `Retry-After = 最早那笔失败 + 窗口 - now`（clamp 到 `[1, 600]`）。成功登录只清
`ipacct` 那一格——同 IP 下别人账号的失败记录必须留着，否则一个合法用户登录成功就能把
攻击者的记录洗白。

Redis 连不上时 **fail-open**（放行 + 打 warning）：限流是附加防线，不能让它变成登录的单点故障。
代价是 Redis 故障期间只剩账号锁定那一层，这个取舍写在代码注释里。

### 3. 客户端 IP

取 `X-Forwarded-For` 首段，没有则用 TCP 对端。**这是信任前提**：生产必须由反向代理
覆盖/剥离客户端自带的 XFF，否则伪造一个头就能绕过 IP 维度。与访问日志用的是同一份取值规则，
所以运维看到的是同一个 IP。

## 响应契约

| 情况 | 状态 | 头 | body |
|---|---|---|---|
| 账号被锁定 | `403` | `Retry-After`、`X-Lock-Remaining`（秒） | `{"error":"forbidden","message":"由于多次登录失败，账户已被锁定 N 秒","details":null}` |
| IP 窗口打满 | `429` | `Retry-After` | `{"error":"too_many_requests","message":"登录尝试过于频繁，请稍后再试","details":null}` |
| 账号或密码错 | `401` | — | `{"detail":"账号或密码错误"}`（FastAPI 默认形状，未改） |

`X-Lock-Remaining` 只是跟 yuxi 对齐的别名，前端只读标准的 `Retry-After`。

**实现细节（坑）**：aegra 的全局 `HTTPException` handler 会重建 `JSONResponse` 但**丢掉
`exc.headers`**（`src/aegra_api/main.py` 的 `agent_protocol_exception_handler`），所以这两个
响应不能 `raise HTTPException(headers=...)`，得直接 `return JSONResponse(..., headers=...)`
（`custom/api/account.py` 的 `_rate_limited`）。另外浏览器要能读到这两个头，
`aegra.json` 的 `http.cors.expose_headers` 必须显式列出（默认只暴露
`Content-Location, Location`）。

## 前端

- `web/src/api/client.ts`：`ApiError` 带 `retryAfter`（解析 `Retry-After`）；登录接口的
  `401` 显示后端的"账号或密码错误"，不再被改写成"登录已过期"。
- `web/src/components/LoginView.vue`：收到锁定/限流后按 `retryAfter` 倒计时，按钮禁用并显示
  "请等待 N 秒"；倒计时结束按钮恢复。

## 已知边界

- **没有按账号名的全局锁定**：换 IP 打同一个账号，靠的是"5 次就锁"那一层（跨 IP 生效）。
- **没有信任名单**：内网 NAT 出口共用 IP 时，30 次/IP 可能误伤同办公室的其他人；
  真遇到就调 `LOGIN_FAILURE_IP_MAX` 或让反代传真实客户端 IP。
- **Redis 掉了只剩账号锁定**，见上面的 fail-open。
- **不做账号解锁接口**：超管改密码不解锁（锁的是登录尝试，不是密码）；等 300 秒自己解。
  真要救急，直接改库 `login_locked_until = NULL`。
- **限流计数不做审计日志**，只有 structlog warning（Redis 故障时）。

## Considered Options

| 方案 | 为什么不 |
|---|---|
| 只做账号锁定（不碰 Redis） | 挡不住密码喷洒；见上表 |
| 只做 IP 限流 | 挡不住代理池盯一个账号 |
| 进程内计数（`cachetools`） | 多副本下阈值变成 `5 × 副本数`，重启即清零 |
| 令牌桶 + 全局配额 | 登录不是稳态流量，滑动窗口的"最近 N 秒失败次数"语义更贴需求，也不需要额外组件 |
| 验证码 / 强制 2FA | 内网工具，先不上；留给"真的被打过"之后再说 |

## Consequences

- `user_tables` 多三列（迁移 `d4e5f6a7b8c9`），每次失败一次小 UPDATE。
- 登录路径依赖 Redis；部署文档要说明"Redis 故障时登录仍可用，只是失去 IP 维度"。
- 前端登录页多了倒计时状态机，`Retry-After` 没有时（例如老后端）行为与以前一致。
