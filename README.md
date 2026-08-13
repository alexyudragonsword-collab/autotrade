# AutoTrade — TradingView 自动交易系统

基于 TradingView 的多市场自动交易系统：**定制选股、策略回测、自动交易、自动提醒**，带完整中文 Web 管理后台。

## 功能

| 模块 | 说明 |
|---|---|
| 📡 信号接入 | TradingView Webhook 告警 → 标准化信号 → 幂等去重（重复告警只执行一次） |
| 🛡️ 风控引擎 | Kill switch、标的白名单、单笔/单标的/总敞口限额、日亏损上限、日订单数、交易时段校验；fail-closed（风控异常一律拒单） |
| 🏦 多券商 | 可插拔适配层：**Paper 模拟**（内置）、**富途 OpenAPI**（A股/港股/美股）、**盈透 IBKR**（美股）；券商离线自动降级拒单不崩溃 |
| 🔍 定制选股 | 基本面（PE/PB/市值…）+ 技术面表达式（`close > SMA(close,20) and RSI(close,14) < 30`），支持定时运行并推送结果 |
| 📈 策略回测 | 自研 bar 级事件驱动引擎（下一根 K 线开盘撮合，无未来函数），收益/年化/夏普/回撤/胜率 + 权益曲线图；**参数网格扫描寻优**；**等权买入持有基准对比（超额收益 α）与月度收益热力表** |
| 🤖 本地策略实盘 | 内置策略不依赖 TradingView：按 cron 定时（或手动）在本地行情上跑 `on_bar`，信号进入与 TV 告警相同的风控/下单管道，与回测共用同一份策略代码；支持**多周期**（日线/60m/15m/5m） |
| 🧺 组合再平衡 | PortfolioStrategy 抽象 + 内置动量轮动（MomentumRotation）：按月/周/日再平衡，目标市值自动换算买卖差额，回测与实盘共用同一份 on_rebalance 代码 |
| 🔗 选股联动 | 选股结果一键：组合回测 / 加入自选（仪表盘展示）/ 推送通知 |
| 🔔 多渠道提醒 | Telegram / 邮件 SMTP / 企业微信 / 钉钉，按事件级别过滤 |
| 💬 Telegram 遥控 | 双向指令机器人：`/status` `/pos` `/orders` `/run 策略名` `/kill` `/resume`，仅响应配置的 chat_id |
| ✋ 手动下单 | 订单页手动下单面板，同样经过风控闸门并留痕（Signal source=manual） |
| 🖥️ Web 后台 | 仪表盘、信号日志、订单持仓、策略管理、选股器、回测中心、风控设置、通知设置 |

## 快速开始

```bash
# 1. 配置
cp .env.example .env   # 修改密码、JWT_SECRET、WEBHOOK_SECRET

# 2. 安装依赖
pip install -e '.[data,dev]'      # 核心 + 数据源 + 测试
# 按需：pip install -e '.[futu]' / '.[ibkr]'

# 3. 构建前端
cd frontend && npm install && npm run build && cd ..

# 4. 启动
make dev    # 或 uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
```

打开 http://localhost:8000 ，用 `.env` 里的账号登录。

### Docker 部署

```bash
cp .env.example .env && vim .env
docker compose -f docker/docker-compose.yml up --build -d
# 需要富途：docker compose -f docker/docker-compose.yml --profile futu up -d
```

## TradingView 接入

1. 登录后台 → **系统设置** → 复制 Webhook 地址（含随机 token）
2. TradingView 创建告警 → Webhook URL 填该地址（需 TV 付费套餐）
3. 告警"消息"框粘贴模板（系统设置页可一键复制）：

```json
{
  "secret": "<你的 WEBHOOK_SECRET>",
  "alert_id": "{{timenow}}-{{ticker}}-buy",
  "strategy": "my_strategy",
  "symbol": "{{ticker}}",
  "exchange": "{{exchange}}",
  "action": "buy",
  "qty": 100,
  "order_type": "market",
  "price": {{close}}
}
```

4. 后台 → **策略管理** 创建同名策略（`my_strategy`），选择模式：
   - `signal_only`：只提醒不下单（**建议先用这个跑几天**）
   - `live`：经风控后真实下单（指定券商与默认数量）

本地冒烟测试：

```bash
curl -X POST "http://localhost:8000/webhook/tradingview/<TOKEN>" \
  -H 'Content-Type: application/json' \
  -d '{"secret":"<WEBHOOK_SECRET>","alert_id":"test-001","strategy":"my_strategy",
       "symbol":"AAPL","exchange":"NASDAQ","action":"buy","qty":10,
       "order_type":"market","price":230.5}'
```

## 实盘前检查单（请务必按顺序）

1. ✅ Paper 模拟盘全流程跑通（信号→成交→通知）
2. ✅ 富途用 `FUTU_TRD_ENV=SIMULATE` / IBKR 用 paper 账户（端口 7497）验证下单与回报
3. ✅ 用极小限额演练风控拦截，演练 kill switch
4. ✅ 确认交易时段、白名单配置无误
5. ⚠️ 然后才切换真实账户。自动交易有真实资金风险，后果自负

## 测试

```bash
pytest -q          # 55 个单测 + 端到端测试（GitHub Actions 自动运行）
```

## 运维

- **迁移**：schema 由 Alembic 管理，启动时自动 `upgrade head`；切 Postgres 装 `pip install -e '.[postgres]'` 并改 `DATABASE_URL`
- **备份**：SQLite 每日自动备份到 `data/backups/`（保留 7 份）；Postgres 请用 pg_dump
- **审计**：管理后台全部写操作记录在 `audit_logs`（`GET /api/audit-logs`），密码/密钥字段自动脱敏

## 文档

- [架构说明](docs/architecture.md)
- [TradingView 配置指南](docs/tradingview-setup.md)
- [券商网关部署（OpenD / IB Gateway）](docs/broker-setup.md)

## ⚠️ 免责声明

本项目仅供学习与研究。自动化交易涉及真实资金风险，任何交易决策与损失由使用者自行承担。
