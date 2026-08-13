# 架构说明

## 总览

```
TradingView 告警 ──POST──▶ /webhook/tradingview/{token}
                              │ token + secret 双重校验（可选 TV IP 白名单）
                              ▼
                        signals/parser  ──▶ NormalizedSignal（symbol 规范化：US.AAPL / HK.00700 / SH.600519）
                              │
                        signals/dedup   ──▶ dedup_key 唯一索引，重复告警幂等丢弃
                              │
                        signals/pipeline
                              │  ① 匹配 StrategyConfig（signal_only=仅提醒 / live=下单）
                              │  ② RiskEngine.check —— 9 条规则全过才放行（fail-closed）
                              ▼
                        execution/OrderManager ──▶ BrokerAdapter（paper / futu / ibkr）
                              │        ▲ 成交回报回调（状态机防乱序）
                              ▼
                        notify/Dispatcher ──▶ Telegram / Email / 企业微信 / 钉钉
```

## 模块

- **brokers/**：`BrokerAdapter` 抽象（connect/place_order/cancel/positions/account/quote + 回报回调）。
  `BrokerManager` 负责注册、按市场路由、30 秒健康检查与自动重连；券商离线时信号被拒绝并通知，系统不崩溃。
- **risk/**：规则清单见 `rules.py`（kill switch、白名单、卖出超持仓、交易时段、单笔金额、单标的持仓、总敞口、日订单数、日亏损）。任何规则抛异常按拒绝处理。所有判定写 `risk_events` 日志。
- **strategy/ + backtest/**：`Strategy.on_bar(ctx)` 基类回测/实盘共用。回测撮合规则：信号在第 i 根收盘产生 → 第 i+1 根开盘价成交（防未来函数），限价单按当日触及判定，当日有效。
- **screener/**：规则为结构化 JSON。技术面表达式经 `ast.parse` 白名单校验（仅 OHLCV 列 + 注册指标函数 + 数字常量），杜绝任意代码执行。
- **data/**：A股/港股用 akshare，美股用 yfinance；日线缓存为 parquet（`data/bars/`），增量更新。
- **调度**（APScheduler）：券商健康检查 30s、订单对账 60s（submitted 超时主动查单）、持仓同步 60s（以券商为准）、paper 限价单检查 20s、选股器按 cron。

## 数据库

SQLite（WAL），`DATABASE_URL` 可切 Postgres。表：signals / orders / trades / positions /
strategy_configs / screener_configs / screen_results / backtest_runs / risk_config /
risk_events / notify_channels / app_settings / users。

## 安全

- Webhook：URL 随机 token（可在设置页轮换）+ body `secret` 双重校验，`compare_digest` 防时序攻击，16KB body 上限
- 面板：单用户 JWT + bcrypt + 登录限速（5 次/分钟）
- 所有密钥走 `.env`（gitignore），数据库不存敏感信息

## 本地策略实盘驱动（迭代2）

`strategy/live.py`：`LiveContext` 用 BarStore 行情 + 本地持仓镜像实现 `StrategyContext`，
调度器按策略配置的 cron（北京时间）驱动 `on_bar`；buy/sell 动作转成
`NormalizedSignal(source="strategy")` 走与 TV 告警完全相同的管道。
去重键含最后一根 K 线日期——同一根日线重复运行（手动补跑/调度重叠）天然幂等。

## 运维（迭代2）

- **Alembic**：`backend/alembic/`，0001 基线按 ORM 元数据建表，后续迁移带存在性检查幂等；
  `init_db()` 启动时自动升级，迁移机制引入前的旧库自动 stamp 后升级
- **审计**：`audit.py` 中间件记录 /api 写操作（登录除外，敏感字段脱敏）
- **备份**：调度器每日 UTC 20:30 备份 SQLite 至 `data/backups/`，保留 7 份
- **CI**：GitHub Actions 跑后端 pytest + 前端构建

## 多周期与组合策略（迭代4）

- **多周期**：`BarStore`/`DataProvider` 带 `interval` 参数（1d/60m/15m/5m），分钟线独立
  parquet 缓存并从缓存末日重拉补盘中缺口；`StrategyConfig.timeframe` / `BacktestRun.timeframe`
  控制实盘与回测取数周期。数据源限制：yfinance 分钟线仅近 60 天；akshare 分钟线仅 A股近期。
- **组合策略**：`strategy/portfolio.py` 定义 `PortfolioStrategy.on_rebalance(ctx)` —— ctx 提供
  跨标的 history/position/price/equity 与 `order_target_value`（目标市值）。回测引擎在再平衡日
  （月/周/日首个交易日）调用一次，目标市值差额转市价单、下一根 bar 撮合；实盘侧将差额换算成
  买卖数量信号进入统一管道（等于自动执行调仓单，逐单过风控）。内置 `MomentumRotation` 动量轮动。

## 已知取舍

- 日亏损基于成交时逐笔落库的 realized_pnl（持仓均价口径），IBKR 会再用 commissionReport
  的 realizedPNL 校正；非会计级但足够风控使用
- 美股基本面走 yfinance Ticker.info 并发拉取 + 当日缓存，universe 大时较慢（建议 ≤50 只）
- 参数扫描上限 60 组合，防止误提交笛卡尔爆炸
