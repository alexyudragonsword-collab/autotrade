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

## 已知取舍

- 日亏损计算用持仓均价近似成本基础（够风控用，非会计级）
- IBKR 佣金未回填到成交明细（commissionReport 事件 v2 再接）
- 美股基本面选股暂缺（yfinance 逐票太慢），A股基本面走 akshare 快照
- 本地策略实盘驱动（LiveContext 定时跑 on_bar）为后续增强；v1 实盘信号以 TV 告警为主
