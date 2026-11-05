# agent运行要求
1. 在进行长时任务时，先使用plan规划任务，并列出任务，完成某一任务的时候要生成简报，告知用户
2. 环境需要安装新的依赖的时候，使用ask-user-question插件询问用户
3. 遇到较新的，没遇见的问题或者技术的时候，使用联网搜索功能获取最新信息
4. 代码尽量只在 `custom/`、`graphs/`、`shared/` 三个包里改（`src/` 是 aegra 原生，尽量不动；测试放 `tests/`；`knowledge/` 是数据）。改 `src/` 或数据目录前先征询用户许可。布局与依赖规则见 `CONTEXT.md`「代码布局」，由 `tests/unit/test_layout.py` 守护：两个包互不 import，`shared/` 不 import 框架。
5. 该项目是后端项目需要考虑安全性，幂等性，并发性等问题

## 规模假设（设计时按这个量级想）

- **知识目录未来会到上百个**（现在 `knowledge/` 只有个位数空间，但按上百个设计）。凡是"遍历/列出/校验全部空间"的写法都要先问一句：上百个时还成立吗——
  - `ls /` 的全量输出会直接进模型上下文（几百条路径 = 几千 token）；
  - 可见范围（`hide ["/"] + show [...]`）的解析是按条目线性走的；
  - Mirage 索引缓存现在按会话隔离（`mirage:index:<owner摘要>-<thread>:`），上百个目录 × 多个会话 = 重复缓存，理论上该全局共享（`/` 用全局前缀、`/uploads` 保持按会话）。

## 知识空间约定

- 知识空间（`knowledge/<space>/`，对象存储侧 `okf/<space>/`）里**只放 md**。表格、xlsx 之类走会话上传（`/uploads`），不要放进知识目录。
- 每个知识目录按 **OKF v0.2** 组织：`AGENT.md`（空间说明与入口）、`index.md`（目录索引）、`log.md`（变更记录），再加主题 md（一个主题一个文件）。

## Agent skills

### Issue tracker

Issue 与 spec 都放在 GitHub Issues，统一用 `gh` CLI 操作。见 `docs/agents/issue-tracker.md`。

### Triage labels

沿用默认五角色词表（`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`），标签字符串与角色同名。见 `docs/agents/triage-labels.md`。

### Domain docs

单上下文（single-context）：根 `CONTEXT.md` + `docs/adr/`。见 `docs/agents/domain.md`。