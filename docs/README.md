# aegra-api docs

本目录是 aegra-api 作为 Deep Agents async subagent 通信面（Agent Protocol，简称 ACP）的文档。旧版需求文档属于已废弃的独立包 `aegra-protocol`（该仓库已不存在）。

> ADR 是决策当时的快照，里面写的 env 名（如 `MAX_BLOB_MB`、`OKF_BACKEND`）现在多数已经搬到仓库根 `config.yaml`（§3.2）；旧环境变量名仍可作为覆盖值使用。以 [configuration.md](./configuration.md) 为准。
>
> ADR 是决策快照，但**路径已尽量对到当前布局**（2026-10 重构后的 `custom/api|service|middleware|db/`、`graphs/access|middleware/`、`shared/`）；只有 `adr/archive/` 里那 6 篇保留当时的写法。布局本身见 [`../CONTEXT.md`](../CONTEXT.md) 的「代码布局」。
> 
> 例外是每份 ADR 顶部的**状态行**：它是唯一必须跟着代码走的字段（`决定已定/实现未开始` → `已实现`），
> 否则会像 ADR-0015/0016 那样，代码早就落地了、状态还停在「实现未开始」。

| 文件 | 内容 |
| - | - |
| [design.md](./design.md) | **需求与设计（§7 五项改动已全部实现，§10 测试全部通过）**：deepagents 实发的五类请求、状态/动作词表三方对照、端点契约、`interrupted_by` 数据模型、状态机、取消与 Supersede 时序、本期改动清单、`aegra.json` 示例、已知限制、测试清单 |
| [configuration.md](./configuration.md) | **配置参考**：`aegra.json` 逐键说明与启动期报错、graph key / `assistant_id` 契约、**配置来源三分（§3.1 aegra 原生环境变量 / §3.2 本项目新增的 `config.yaml` / §3.3 密钥）**、日志（stdout + `logs/aegra.log` 轮转与保留）、OpenTelemetry / Prometheus、排错对照表 |
| [streaming.md](./streaming.md) | **流式参考**：`client.stream` 结构与全部 9 种模式实测矩阵、aegra 模式处理与 `version="v2"` 硬编码、deepagents/`astream_events` 适配、v3 进程内✅/HTTP❌、前端 vs 调试选型、子图流不回传等坑、子 agent 流代理与 `async_tasks` 僵尸状态 |
| [frontend-api.md](./frontend-api.md) | **前端接口参考**：`custom/api/` 下的前端接口（`account.py` 注册/登录/whoami + `graphs.py` 图清单 + `spaces.py` 空间与文件树/成员 + `tasks.py` 任务状态/流，共 10 个）+ 3 个内部接口（`delivery.py` 投递 ack 两个、`tasks.py` 取消）、鉴权与 token 时长、原生接口清单、SSE 帧契约（增量 / interrupt-resume / 回写消息识别）、定时任务实测行为、会话删除与已知限制 |
| [S3.md](./S3.md) | **对象存储操作手册**：起 SeaweedFS、全部相关 env（大小/配额/缓存/镜像）、「本地文件怎么进 S3」（OKF 推送脚本 / 运维直连）、key 布局、校验与排错、清理与容量对账 |
| [adr/](./adr/README.md) | **决定记录**：现行 8 篇 + 历史 6 篇（`adr/archive/`）。索引里按编号列出「一句话结论 + 代码落点」，并给了按主题的阅读顺序（权限与空间 / 任务投递 / 配额） |

其他相关文档：

- 术语表与代码布局：[`../CONTEXT.md`](../CONTEXT.md)（「代码布局」一节讲三个包的层与依赖方向）
- 配置参考：[`configuration.md`](./configuration.md)
- 快速上手与运行命令：[`../README.md`](../README.md)
