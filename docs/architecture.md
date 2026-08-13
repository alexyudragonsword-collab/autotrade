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

## 多账户与持仓守护（迭代5）

- **多账户**：`broker_accounts` 表存账户实例（name 即全系统 broker 标识），适配器全部参数化
  可多实例；启动按表构建，表空时从 env 播种默认账户（向后兼容）；Web 增删/启停账户即时
  注册/断开（新账户自动挂接成交回报回调）。密钥仍走 env。有持仓的账户禁止删除。
- **持仓守护**：`risk/guard.py` 每分钟巡检 qty>0 持仓，维护 high_water_price；
  止损（较成本亏损%）/止盈（较成本盈利%）/移动止损（距高水位回撤%）任一触发即市价全平，
  Signal(source=risk_guard) 留痕 + 警告通知。守护单只减少敞口故不过限额规则，但服从
  kill switch；在途卖单存在时不重复触发。

## 策略在线编辑器与回测走查（迭代6）

- **自定义策略**：`strategy/custom.py` —— 代码在预置命名空间 exec（pd/np/指标函数/两种策略
  基类），要求恰好定义一个策略类；保存前合成行情试跑回测拦截运行期错误；registry 先查内置
  再查自定义（updated_at 变化即热重编译）。被策略配置引用的自定义策略禁止删除。
  ⚠️ exec 即完整 Python 权限——自托管单用户系统，仅登录管理员可写，勿粘贴不明代码。
- **走查图**：`GET /api/backtests/{id}/chart?symbol=` 返回该回测周期的 K 线 + 逐笔买卖点，
  前端 ECharts 蜡烛图标注（dataZoom 缩放复盘）。

## 绩效归因与通知路由（迭代7）

- **绩效**：`api/performance.py` —— 基于成交时落库的 realized_pnl，按 Signal.strategy_name /
  Order.broker / Order.symbol 三维归因（盈亏/平仓次数/胜率/手续费）+ 每日盈亏序列；
  `account_snapshots` 表每 4 小时记录各在线账户净值（同日 upsert），绩效页画净值曲线。
- **通知路由**：NotifyEvent 携带 strategy/broker 元数据，渠道 config 存
  {"strategies": [...], "brokers": [...]}（空=不限）；事件缺失元数据（kill switch 等系统级）
  时投递到所有渠道。典型用法：策略 A 的消息进 Telegram 群 A，实盘账户的消息单独发邮件。

## 实时推送与发布（迭代8）

- **WebSocket**：`events.py` 进程内事件总线（慢消费者丢最旧消息不阻塞业务）；
  `/ws?token=<JWT>` 推送 signal / order_update / notify 三类事件 + 25s ping 保活；
  前端自动重连（指数退避），通知事件即时弹窗，仪表盘/订单页实时刷新（轮询降频为兜底）。
- **镜像发布**：push 到 main / 打 v* tag 时 GitHub Actions 构建镜像发布到
  ghcr.io/<owner>/<repo>（含 buildx 缓存）。
- **报告导出**：`backtest/report.py` 生成自包含 HTML（内联 SVG 曲线，零外部依赖）。

## 期权交易（迭代9）

- **合约模型**：`domain/contracts.py` —— 规范符号 `US.AAPL|20250919|C|230`（保留市场前缀，
  既有路由零改动）；乘数全链路核算（风控名义、成交盈亏、账户净值、浮动盈亏）。
- **空头持仓**：Position.qty 可为负（期权卖开），paper 支持带符号撮合（股票仍禁做空）；
  已实现盈亏泛化为"减仓成交"计算（卖出减多仓/买入回补空仓），并修复了全平后盈亏丢失的问题。
- **卖方风控两档**：默认 `CoveredOrSecuredRule`（卖 Call 需足额正股备兑、卖 Put 需现金担保，
  现金取实时账户→净值快照→fail-closed）；开启 `allow_naked_selling` 后由
  `NakedNotionalRule`（Σ 行权价×乘数×张数 ≤ 上限）约束。买入平空视为减风险，不受限额规则。
- **守护**：空头方向自动反转（涨破止损/低水位反弹移动止损→买回平仓）；
  `run_expiry_guard`（每4小时）到期前 N 天每日提醒 + 可选到期前 1 日自动平仓（dedup 幂等）。
- **券商**：IBKR `Option` 合约 + qualify 缓存 + `reqSecDefOptParams` 链（10 分钟缓存、
  strikes_around 限幅节流）；富途美股期权代码可逆构造、**HK 期权代码经链查询建双向缓存**
  （未加载时下单会提示先查链）；IB 期权 avgCost 为每张口径，入库已折算为每股。
- **入口**：TV 告警 expiry/strike/right、手动下单双形态、期权链页面点击预填。
- **仍在范围外**：期权回测（无数据源）、希腊字母/IV、组合腿单、行权/被行权
  （被行权后的正股变动经持仓同步自然体现）。

## 已知取舍

- 日亏损基于成交时逐笔落库的 realized_pnl（持仓均价口径），IBKR 会再用 commissionReport
  的 realizedPNL 校正；非会计级但足够风控使用
- 美股基本面走 yfinance Ticker.info 并发拉取 + 当日缓存，universe 大时较慢（建议 ≤50 只）
- 参数扫描上限 60 组合，防止误提交笛卡尔爆炸
