# AutoTrade — TradingView 自动交易系统

基于 TradingView 的多市场自动交易系统：**定制选股、策略回测、自动交易、自动提醒**，带完整 Web 管理后台（**中英文双语一键切换**）。

## 功能

| 模块 | 说明 |
|---|---|
| 📡 信号接入 | TradingView Webhook 告警 → 标准化信号 → 幂等去重（重复告警只执行一次） |
| 🛡️ 风控引擎 | Kill switch、标的白名单、单笔/单标的/总敞口限额、日亏损上限、日订单数、交易时段校验；fail-closed（风控异常一律拒单） |
| 🏦 多券商多账户 | 可插拔适配层：**Paper 模拟**（内置）、**富途 OpenAPI**（A股/港股/美股）、**盈透 IBKR**（美股）；同类型可配多个账户实例（如富途模拟+实盘并存），Web 页面增删账户即时生效；券商离线自动降级拒单不崩溃 |
| 🛑 持仓守护 | 止损 / 止盈 / 移动止损（高水位回撤）：每分钟巡检持仓，触发即市价平仓并推送通知；守护单服从 kill switch；**空头持仓（期权卖方）方向自动反转**（涨破止损买回平仓、低水位反弹移动止损） |
| 🎯 期权交易 | **买方与卖方（sell-to-open）都支持，IBKR + 富途**：期权链页面（到期日/行权价矩阵/ATM 高亮/点击下单）、合约乘数全链路核算、两档卖方风控（默认仅备兑 Call/现金担保 Put，显式开启裸卖后受空头名义限额约束）、到期提醒 + 可选到期前自动平仓、TV 告警可带 expiry/strike/right 直接驱动期权下单 |
| 🎡 期权内置策略 | **CoveredCall 备兑开仓**（滚动卖虚值 Call 收权利金，临近到期自动买回滚动）、**CashSecuredPut 现金担保卖 Put**（纯 Put 腿滚动收权利金，接货后正股自行处置）、**WheelStrategy 车轮**（现金担保卖 Put ↔ 被行权接货后自动切换备兑 Call）：按 cron 自动运行，实时查链按虚值比例/到期区间选合约，走统一风控管道；paper 执行 + 真实券商链报价可安全模拟验证 |
| 🔍 定制选股 | 基本面（PE/PB/市值…）+ 技术面表达式（`close > SMA(close,20) and RSI(close,14) < 30`），支持定时运行并推送结果 |
| 📈 策略回测 | 自研 bar 级事件驱动引擎（下一根 K 线开盘撮合，无未来函数），收益/年化/夏普/回撤/胜率 + 权益曲线图；**参数网格扫描寻优**；**等权买入持有基准对比（超额收益 α）与月度收益热力表** |
| 🤖 本地策略实盘 | 内置策略不依赖 TradingView：按 cron 定时（或手动）在本地行情上跑 `on_bar`，信号进入与 TV 告警相同的风控/下单管道，与回测共用同一份策略代码；支持**多周期**（日线/60m/15m/5m） |
| 📚 内置策略库 | 股票单标的 7 个：双均线 SmaCross、MACD 趋势、RSI 回归、布林带回归、唐奇安突破（海龟）、网格交易、定投 DcaInvest；组合 1 个：动量轮动；期权 3 个：备兑/现金担保Put/车轮——全部可回测（期权除外）、可实盘、可在编辑器派生改写 |
| 🧺 组合再平衡 | PortfolioStrategy 抽象 + 内置动量轮动（MomentumRotation）：按月/周/日再平衡，目标市值自动换算买卖差额，回测与实盘共用同一份 on_rebalance 代码 |
| ✏️ 策略在线编辑器 | Web 上直接写 Python 策略（单标的/组合均可），保存前自动编译 + 合成行情试跑校验，保存即热加载——立刻可回测、可绑定实盘，无需重启 |
| 🕯️ 回测走查 | 单标的 K 线蜡烛图逐笔标注买卖点（红买绿卖），缩放拖动复盘每一笔交易 |
| 🔗 选股联动 | 选股结果一键：组合回测 / 加入自选（仪表盘展示）/ 推送通知 |
| 🔔 多渠道提醒 | Telegram / 邮件 SMTP / 企业微信 / 钉钉，按事件级别过滤 + **按策略/账户路由**（不同策略的消息发到不同群；系统级事件始终全渠道投递） |
| 💰 绩效归因 | 实盘已实现盈亏按**策略/账户/标的**归因（盈亏/胜率/手续费），每日盈亏柱状图 + 账户净值曲线（每 4 小时自动快照） |
| 💬 Telegram 遥控 | 双向指令机器人：`/status` `/pos` `/orders` `/run 策略名` `/kill` `/resume`，仅响应配置的 chat_id |
| ✋ 手动下单 | 订单页手动下单面板，同样经过风控闸门并留痕（Signal source=manual） |
| 🖥️ Web 后台 | 仪表盘、信号日志、订单持仓、策略管理、策略编辑器、选股器、回测中心、绩效分析、风控设置、通知设置；**WebSocket 实时推送**——信号/成交/告警即时刷新并弹窗，无需手动刷新 |
| 🌐 双语界面 | 中文 / English 一键切换（右上角），刷新后保持所选语言；Element Plus 组件（日期选择/分页等）与图表、消息提示同步切换 |
| 📄 报告导出 | 回测一键导出自包含 HTML 报告（指标/权益曲线/月度收益/交易明细），离线可看、可直接分享 |

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
pytest -q          # 145 个单测 + 端到端测试（GitHub Actions 自动运行）
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
