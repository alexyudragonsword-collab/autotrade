# 券商网关部署指南

## Paper 模拟盘（默认启用）

无需任何外部依赖。`PAPER_ENABLED=true`，初始资金 `PAPER_INITIAL_CASH`（默认 100 万）。
撮合价：真实券商实时行情 → 本地日线缓存收盘价 → 信号携带的 price，三级兜底。

## 富途 OpenAPI

1. **开通**：牛牛账号开通 OpenAPI 权限（富途官网 → OpenAPI）
2. **运行 OpenD 网关**（下单请求经它转发）：
   - 官网下载 OpenD，首次启动需账号 + 手机验证码，**建议先在桌面环境交互式登录一次**
   - 之后可用配置文件免交互启动；容器化可参考 `docker-compose.yml` 的 `futu` profile
3. **配置 .env**：

```ini
FUTU_ENABLED=true
FUTU_OPEND_HOST=127.0.0.1   # 容器内为 futu-opend
FUTU_OPEND_PORT=11111
FUTU_TRD_ENV=SIMULATE       # 先模拟！确认无误后改 REAL
FUTU_UNLOCK_PWD=交易密码     # REAL 环境必填
```

4. `pip install 'autotrade[futu]'`，重启服务，后台"系统设置"页应显示 futu 在线

注意：
- A股实盘需开通相应交易权限；模拟环境（SIMULATE）即可完整验证流程
- OpenD 掉线时系统自动每 30 秒重连，期间该券商信号被拒绝并通知

## 盈透 IBKR

1. **运行 TWS 或 IB Gateway**，并在设置中启用 API：
   - Configuration → API → Settings → ✅ Enable ActiveX and Socket Clients
   - Paper 账户端口 **7497**，真实账户 **7496**
   - 无人值守推荐 [gnzsnz/ib-gateway](https://github.com/gnzsnz/ib-gateway-docker) 镜像（内置 IBC 自动登录）
2. **配置 .env**：

```ini
IBKR_ENABLED=true
IBKR_HOST=127.0.0.1          # 容器内连宿主机: host.docker.internal
IBKR_PORT=7497               # 先 paper！
IBKR_CLIENT_ID=17            # 同一 Gateway 的多个客户端需不同 ID
```

3. `pip install 'autotrade[ibkr]'`，重启服务

注意：
- Gateway 每日定时重启，系统健康检查会自动重连
- API 消息经内置令牌桶节流（40 msg/s），规避 pacing violation
- `clientId` 冲突会连接失败，换一个数字即可

## 实盘切换检查单

1. Paper 全流程 ✅（信号→风控→成交→通知）
2. Futu SIMULATE / IBKR 7497 下单成功且收到回报 ✅
3. 极小限额验证风控拦截、kill switch 演练 ✅
4. 风控限额、白名单、交易时段核对 ✅
5. 切 `FUTU_TRD_ENV=REAL` / `IBKR_PORT=7496`，从最小仓位开始
