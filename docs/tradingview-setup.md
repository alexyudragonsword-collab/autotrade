# TradingView 配置指南

## 前提

- TradingView **付费套餐**（Essential 及以上）才支持 Webhook 告警
- 服务器需公网可达（建议 HTTPS 反代，如 caddy / nginx）

## 步骤

### 1. 获取 Webhook 地址

后台 → 系统设置 → 复制 Webhook 地址，形如：

```
https://你的域名/webhook/tradingview/AbCdEf123...
```

Token 泄露可在设置页一键重置。

### 2. 创建告警

- 图表上创建 Alert，条件选择你的指标/策略（如 Pine 策略的 `alert()` / 订单成交事件）
- 勾选 **Webhook URL**，粘贴上面的地址
- **消息（Message）** 填 JSON 模板：

```json
{
  "secret": "<.env 里的 WEBHOOK_SECRET>",
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

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| secret | ✅ | 与服务器 `WEBHOOK_SECRET` 一致 |
| alert_id | 建议 | 去重键；不填则按"同分钟同策略同标的同方向"兜底去重 |
| strategy | ✅ | 与后台"策略管理"中的策略名一致 |
| symbol / exchange | ✅ | 用 `{{ticker}}` / `{{exchange}}` 占位符即可 |
| action | ✅ | `buy` / `sell` / `close`（close=清空该标的持仓） |
| qty | 可选 | 不填用策略配置的默认数量 |
| order_type | 可选 | `market`（默认）/ `limit`（须带 price） |
| price | 限价必填 | 市价单时作为风控估值与 paper 撮合参考价 |

买/卖各建一个告警（action 不同）。Pine 策略可在 `alert_message` 里动态填 action。

### 3. 后台创建同名策略

策略管理 → 新建，策略名与 `strategy` 字段一致：

- 先用 **signal_only** 模式观察几天信号质量
- 确认无误后切 **live**，指定券商（先 paper → 再券商模拟环境 → 最后真实）

## 交易所映射

| TV exchange | 内部市场 | symbol 示例 |
|---|---|---|
| NASDAQ / NYSE / AMEX / BATS | US | `US.AAPL` |
| HKEX | HK | `HK.00700`（自动补零） |
| SSE | CN | `SH.600519` |
| SZSE | CN | `SZ.000001` |

## 注意事项

- **TV 3 秒超时会重发** → 系统秒回 200 并异步处理，重复告警由 `alert_id` 幂等去重
- **同一根 K 线可能双触发** → 同上，勿担心重复下单
- TV 官方出口 IP（52.89.214.238 / 34.212.75.30 / 54.218.53.128 / 52.32.178.7）可在
  `.env` 设 `TV_IP_ALLOWLIST_ENABLED=true` 启用白名单（IP 可能变化，默认关闭）
