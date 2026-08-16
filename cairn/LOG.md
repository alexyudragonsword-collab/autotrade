# Project Cairn 日志

本文件按倒序记录实质性进展——最新条目在最上、紧贴本行之下。每条保持简短——只写摘要与指针；结论沉淀进 `cairn/<topic>.md`。

## 2026-08-16 · Project Cairn 初始化

- 初始化 Project Cairn 结构（git_policy: track；provider 暂缓对接；语言中文）。
- 历史迁移模式：`inventory_only`。
- 原 `CLAUDE.md` 的工程规则并入 `AGENTS.md`（「工程硬性约定」等节），`CLAUDE.md` 改为一行 `@AGENTS.md` 桩。
- 详见 `AGENTS.md` 与 `.cairn/config.yaml`。

## 2026-08-16 · 历史盘点（inventory_only）

- 初始化前项目已有 13 轮迭代（MVP → 期权 → 内置策略扩充 → 双语 GUI → 文档补全），演进史见 `CHANGELOG.md`，逐迭代设计决策见 `docs/architecture.md` 各章节。
- 既有知识载体盘点：`docs/` 7 份专题文档（架构/策略开发/期权/后台手册/FAQ/券商/TV 接入）、`CHANGELOG.md`、`ROADMAP.md`（产品路线图，含「明确不做」边界）、后端 85% 模块含中文 docstring。
- 上述文档保持原位不迁入 `cairn/`（工程/产品文档归代码树管理）；`cairn/` 从零开始，仅记录此后的协作知识。
- 当前状态：后端 145 测试全绿；main 与开发分支同步于「新增 ROADMAP.md」提交。
