# FAQ 与故障排查

## 信号与下单

**Q：TV 告警发了，但系统没下单，怎么排查？**

按这条链路从上往下查：

1. **信号到了吗**——信号日志页有没有这条记录？没有 → 检查 TV 告警的 Webhook URL（含 token）是否正确、服务器公网是否可达（TV 需要 HTTPS）、告警是否真的触发了（TV 的告警历史）；
2. **被判重复了吗**——状态"重复"说明 `alert_id` 生成的去重键撞了。TV 模板里用 `{{timenow}}` 参与拼接可避免；
3. **匹配到策略了吗**——状态"已拒绝"且说明含"策略未找到/未启用"→ 策略管理里的名称必须与告警 `strategy` 字段**完全一致**，且已启用；
4. **被风控拦了吗**——状态"风控拦截"，说明列写明了具体规则（白名单/限额/时段/kill switch…）→ 风控设置页调整或确认这是期望行为；
5. **模式对吗**——`signal_only` 模式只提醒不下单，这是新策略的默认建议模式；
6. **券商在线吗**——仪表盘看账户状态；离线时信号被拒并通知（系统不崩溃，恢复在线后新信号正常）。

**Q：本地策略到点没运行？**

- cron 是**北京时间**；策略配置需填了本地策略类 + 监控标的 + cron 三项且已启用；
- 用策略行的「运行」按钮手动触发一次看日志——数据不足、指标窗口不够时策略会静默返回（属正常）；
- 同一根 K 线只会执行一次（幂等），当日已跑过再手动触发不会重复下单。

**Q：手动下单也被拒？**
手动单走同一套风控。看返回的拒绝原因，最常见：标的不在白名单、超单笔金额上限、非交易时段、期权未开启期权交易开关。

## 券商

**Q：富途显示离线？**
OpenD 网关没连上：确认 OpenD 进程活着、`FUTU_OPEND_HOST/PORT` 正确（Docker 内是 `futu-opend` 服务名）、OpenD 登录态有效（验证码过期需重登）。系统每 30 秒健康检查并自动重连。

**Q：IBKR 显示离线？**
TWS / IB Gateway 未运行或 API 未启用：TWS → Global Configuration → API → Enable ActiveX and Socket Clients；端口对上（paper 7497 / live 7496）；Docker 部署时 `IBKR_HOST=host.docker.internal`。注意 IB 网关每日自动重启窗口会短暂离线。

**Q：真实账户下单前要做什么？**
按 README「实盘前检查单」顺序来：Paper 全流程 → 券商模拟环境（`FUTU_TRD_ENV=SIMULATE` / IB paper）→ 小限额演练风控与 kill switch → 核对时段白名单 → 才切真实。富途真实环境必须配 `FUTU_UNLOCK_PWD`。

## 数据

**Q：回测/选股拉不到数据？**
- 美股走 yfinance：服务器需能访问 Yahoo（中国大陆机房不可达，选香港/新加坡节点）；偶发限流稍后重试；
- A股/港股走 akshare：接口偶有变动，报错会原样透传；升级 akshare（`pip install -U akshare`）常能解决；
- 分钟线覆盖有限：yfinance 近 60 天（最小 5m）、akshare 仅 A股近期——分钟级回测区间别设太长；
- 缓存在 `data/bars/`（parquet），怀疑缓存脏了可删除对应文件重拉。

**Q：期权链页面空白？**
所选账户必须是**在线的 IBKR 或富途**（paper 无链数据）；HK 期权需要 OpenD 有期权行情权限。期权策略在 paper 账户上运行时会自动借用在线真实券商的链（见 [options.md](options.md) §7）。

## 风控与守护

**Q：kill switch 触发后怎么恢复？**
仪表盘交易总开关重新打开，或 Telegram 发 `/resume`。触发原因（如日亏损达上限）在风控事件里可查；日亏损上限触发的，次日自动重新计数。

**Q：期权卖单被拒"现金担保不足"，但账户有钱？**
现金实时查询超时会 fail-closed 拒单（宁可错杀）。检查券商网关连通性；账户净值快照（每 4 小时）落库后可作兜底数据源。

**Q：守护平仓单会被限额拦住吗？**
不会。守护/平仓类操作视为减风险，不受仓位限额约束，但**服从 kill switch**。

## 运维

**Q：数据备份与恢复？**
SQLite 每日自动备份到 `data/backups/`（保留 7 份）。恢复：停服务 → 用备份文件覆盖 `data/autotrade.db` → 启动。整个 `data/` 目录 + `.env` 就是全部状态，迁移服务器打包这两样即可。

**Q：升级版本？**
Docker：`docker compose pull && docker compose up -d`（数据库迁移启动时自动执行）。源码：`git pull` → 重装依赖（若 pyproject 变了）→ `cd frontend && npm run build` → 重启服务。

**Q：忘记后台密码？**
改 `.env` 的 `ADMIN_PASSWORD` 后重启——启动时会同步管理员密码。

**Q：日志在哪？**
标准输出（Docker: `docker compose logs -f app`；systemd: `journalctl -u autotrade`）。后台的审计日志（`GET /api/audit-logs`）记录全部写操作。

**Q：Webhook token 泄露了？**
系统设置页「轮换」生成新 token（旧地址立即失效），同时更换 `WEBHOOK_SECRET` 并更新 TV 告警模板。
