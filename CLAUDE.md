# CLAUDE.md

TradingView 自动交易系统：FastAPI 后端 + Vue3 前端，多券商（Paper/富途/IBKR）、股票 + 期权（含卖方）、回测/选股/风控/通知全流程。架构细节读 `docs/architecture.md`，本文件只写"改代码前必须知道的事"。

## 常用命令

```bash
# 后端测试（必须从 backend/ 目录运行）
cd backend && python -m pytest tests -q          # 全量，当前 145 个
cd backend && python -m pytest tests/test_options.py -q   # 单文件

# 前端构建（改了 frontend/ 后必须构建通过才算完）
cd frontend && npm run build

# 本地起服务（需先 cp .env.example .env）
uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
```

## 硬性约定

- **注释/文档/提交信息用中文**；代码标识符用英文。
- **风控 fail-closed**：风控路径上的任何异常都必须落在"拒单"分支，绝不允许异常导致放行。改 `risk/` 时保持这一不变式。
- **Alembic 迁移幂等**：新迁移沿用既有 guard 模式（先检查列/表是否存在再操作）；`0001` 基线是 `create_all`，所以**新表/新列要同时改 `db/models.py` 和新迁移文件**，测试库走 0001 自动获得。
- **期权符号**：`US.AAPL|20250919|C|230`，保留市场前缀（`split(".", 1)[0]` 路由依赖它）。乘数必须全链路传递（OrderRequest→Order→Position→风控→盈亏）。
- **信号幂等**：一切自动下单入口都要有 dedup_key；本地策略的去重键含最后一根 K 线日期。
- **前端 i18n**：中文原文即词条 key。模板里 `$t('中文')`，脚本里 `tr('中文')`（从 `../i18n` 导入）；新增用户可见文案要在 `frontend/src/i18n.js` 的 en 词典补一行英文（漏补只是英文模式显示中文，不报错）。en 词典值里禁用 `{` `}` `|` `@`（vue-i18n 特殊语法）。
- **策略回测/实盘同码**：`Strategy.on_bar` / `PortfolioStrategy.on_rebalance` 两用；期权 `OptionStrategy.on_run` 实盘专属（async），回测 API 对期权策略显式报错。
- **期权策略单轮单向**：同一次 on_run 不得既买回又开新仓（与持仓同步竞态）。

## 测试注意

- 测试自带 SQLite 临时库（走 0001 基线建表），不需要外部服务；券商用 FrozenStore/stub 模式。
- Paper 撮合顺序是"先发成交回报再更新持仓"——依赖此顺序的测试勿改动次序。
- yfinance/akshare 在测试中全部 mock，不要写真实网络调用的测试。

## 文档地图

| 文件 | 内容 |
|---|---|
| `docs/architecture.md` | 模块职责、数据流、各迭代设计决策（改架构先读这个） |
| `docs/strategy-development.md` | 三类策略基类与 ctx API、指标白名单 |
| `docs/options.md` | 期权符号/风控两档/到期守护/券商细节 |
| `docs/webui-guide.md` / `docs/faq.md` | 面向用户的操作与排查 |
| `docs/broker-setup.md` / `docs/tradingview-setup.md` | 券商网关与 TV 接入 |
| `CHANGELOG.md` | 迭代演进史 |
| `ROADMAP.md` | 规划方向与明确不做的事（新需求先对照这里） |

## 交付流程

改动完成的定义：后端 `pytest` 全绿 + 前端 `npm run build` 通过 + 相关文档同步更新（尤其 `docs/architecture.md` 的规则数量/策略清单这类易漂移处）+ README 功能表若有新特性补一行。演示用的 `data/`、`.env` 不入库（已 gitignore）。
