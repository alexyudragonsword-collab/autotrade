# 策略开发指南

本系统的策略分三类，共用"回测与实盘同一份代码"的设计（期权策略除外，见下表）：

| 类型 | 基类 | 入口方法 | 视角 | 回测 | 实盘 |
|---|---|---|---|---|---|
| 单标的策略 | `Strategy` | `on_bar(ctx)` | 一次处理一只标的的一根 K 线 | ✅ | ✅ |
| 组合策略 | `PortfolioStrategy` | `on_rebalance(ctx)` | 再平衡日拿到全部标的，统一调仓 | ✅ | ✅ |
| 期权策略 | `OptionStrategy` | `async on_run(ctx)` | 单一正股 + 其期权持仓 + 实时期权链 | ❌（无期权历史数据） | ✅ |

开发路径有两条：

- **在线编辑器**（推荐）：后台「策略编辑器」页直接写，保存前自动编译 + 合成行情试跑校验，保存即热加载——立刻可回测、可绑定实盘，无需重启服务；
- **源码内置**：加文件到 `backend/app/strategy/builtin/` 并在 `registry.py` 注册（需要改动仓库时才用）。

> ⚠️ 编辑器里的代码以完整 Python 权限 `exec` 执行。本系统定位是自托管单用户部署，编辑器仅登录管理员可用——**不要粘贴来源不明的策略代码**。

## 1. 单标的策略（Strategy）

### 最小示例

```python
class MyStrategy(Strategy):
    """收盘价上穿 20 日均线买入，下穿卖出。"""

    params = {"period": 20, "qty": 100}

    def on_bar(self, ctx):
        n = int(self.p["period"])
        bars = ctx.history(n + 2)
        if len(bars) < n + 1:          # 数据不足直接返回，别让指标算出 NaN
            return
        ma = SMA(bars["close"], n)
        above = bars["close"].iloc[-1] > ma.iloc[-1]
        was_below = bars["close"].iloc[-2] <= ma.iloc[-2]
        if above and was_below and ctx.position() == 0:
            ctx.buy(self.p["qty"])
        elif not above and ctx.position() > 0:
            ctx.close()
```

### 生命周期与参数

- `params`（类属性）声明全部可调参数及默认值；实例化时被覆盖项合并进 `self.p`。回测页的"参数(JSON)"、策略配置里的 params、参数扫描都通过这一机制注入。
- `on_start(ctx)` / `on_stop(ctx)` 可选，回测开始/结束各调一次。
- `on_bar(ctx)` 每根 K 线调用一次（实盘则是每次调度触发时对最新一根调用）。

### StrategyContext API

| 成员 | 说明 |
|---|---|
| `ctx.symbol` | 当前标的（规范符号，如 `US.AAPL`） |
| `ctx.history(n)` | 当前时点（含）往前 n 根 K 线的 DataFrame，列 `open/high/low/close/volume`，索引为时间。**永远不含未来数据** |
| `ctx.position()` | 当前持仓数量，0 = 空仓 |
| `ctx.buy(qty, limit=None)` / `ctx.sell(qty, limit=None)` | 市价/限价买卖 |
| `ctx.close()` | 平掉全部多头持仓 |
| `ctx.log(msg)` | 记录日志（回测时进报告，实盘时进服务日志） |

### 撮合规则（回测引擎）

- 信号在第 i 根 K 线**收盘后**产生 → 第 i+1 根**开盘价**成交（防未来函数）；
- 限价单按当日最高/最低价是否触及判定，当日有效；
- 佣金按系统设置的费率计入。

实盘侧：`buy/sell` 转成 `NormalizedSignal(source="strategy")`，走与 TradingView 告警**完全相同**的风控/下单管道。去重键含最后一根 K 线日期，同一根 K 线上重复运行（手动补跑、调度重叠）天然幂等——所以 `on_bar` 里不需要自己防重。

## 2. 组合策略（PortfolioStrategy）

跨标的调仓，思路是"每个再平衡日给出各标的的**目标市值**，引擎自动换算买卖差额"：

```python
class MyRotation(PortfolioStrategy):
    """涨幅前 2 名等权持有。"""

    params = {"rebalance": "monthly", "lookback": 120, "top_k": 2, "reserve": 0.02}

    def on_rebalance(self, ctx):
        scores = {}
        for sym in ctx.symbols:
            bars = ctx.history(sym, self.p["lookback"] + 1)
            if len(bars) < self.p["lookback"] // 2:
                continue
            scores[sym] = bars["close"].iloc[-1] / bars["close"].iloc[0] - 1
        winners = sorted(scores, key=scores.get, reverse=True)[: self.p["top_k"]]
        target = ctx.equity() * (1 - self.p["reserve"]) / max(len(winners), 1)
        for sym in ctx.symbols:
            ctx.order_target_value(sym, target if sym in winners else 0.0)
```

### PortfolioContext API

| 成员 | 说明 |
|---|---|
| `ctx.symbols` | 本次参与的全部标的 |
| `ctx.history(symbol, n)` | 某标的往前 n 根 K 线 |
| `ctx.position(symbol)` / `ctx.price(symbol)` | 持仓数量 / 最新收盘价 |
| `ctx.equity()` | 总权益 = 现金 + 全部持仓市值 |
| `ctx.order_target_value(symbol, value)` | 调仓到目标市值，0 = 清仓 |

- `params["rebalance"]` 控制频率：`daily` / `weekly` / `monthly`（默认），在每个新周期的**首个交易日**触发；
- 预留一点现金（如示例的 `reserve: 0.02`）付手续费，避免满仓单因现金不足被缩量。

实盘侧目标市值差额被换算成买卖数量信号逐单进入管道——**每一笔调仓单都单独过风控**。

## 3. 期权策略（OptionStrategy）

实盘专属（无期权历史数据源，不进回测引擎）。`on_run` 是 **async**，运行时实时查期权链选合约。详细的期权机制（符号、乘数、卖方风控两档）见 [options.md](options.md)。

```python
class MyPutSeller(OptionStrategy):
    """无空头 Put 时卖出一张 30~45 天、虚值 5% 的现金担保 Put。"""

    params = {"otm_pct": 5.0, "min_dte": 30, "max_dte": 45, "roll_dte": 3}

    async def on_run(self, ctx):
        shorts = self.short_positions(ctx, "P")
        for pos in shorts:                       # 临近到期先买回（滚动）
            if pos.dte <= self.p["roll_dte"]:
                ctx.buy_close(pos.symbol, abs(pos.qty), multiplier=pos.multiplier)
                return                           # 单轮只做一个方向，开新仓等下轮
        if shorts:
            return
        item = await ctx.select_contract("P", self.p["min_dte"],
                                         self.p["max_dte"], self.p["otm_pct"])
        if item is None:
            return
        price = self.mid_or_last(item)
        if price is not None:
            ctx.sell_open(item.symbol, 1, price=price, multiplier=item.multiplier)
```

### OptionStrategyContext API

| 成员 | 说明 |
|---|---|
| `ctx.underlying` | 正股符号（策略配置 symbols 里的每一项各调用一次 `on_run`） |
| `await ctx.spot()` | 正股最新价 |
| `ctx.stock_qty()` / `ctx.cash()` | 正股持仓 / 执行账户现金（可能为 None） |
| `ctx.option_positions()` | 该标的全部期权持仓（`OptionPosition`：contract/qty/avg_cost/multiplier/dte，qty<0 为空头） |
| `await ctx.expirations()` | 可用到期日列表 |
| `await ctx.select_contract(right, min_dte, max_dte, otm_pct)` | 按规则选合约：dte 区间内最近到期日；Call 取 ≥ spot×(1+otm%) 最低档、Put 取 ≤ spot×(1−otm%) 最高档。选不出返回 None |
| `ctx.sell_open(...)` / `ctx.buy_close(...)` | 卖开 / 买平（转信号进统一管道，风控照常把关） |

基类助手：`self.short_positions(ctx, "C"/"P")` 过滤空头持仓；`self.mid_or_last(item)` 取买卖中间价（无双边报价退化为 last）。

**重要模式——单轮单向**：同一次运行里不要既买回又开新仓（买回后的持仓/担保额度要等持仓同步才更新，同轮开仓会与风控检查竞态）。内置三个策略都遵循"先滚动买回、开新仓留给下一轮"。

**链数据来源**：优先执行账户；执行账户不支持期权链（如 paper）时自动借用任一在线真实券商的链——"**paper 执行 + 真实链报价**"是验证期权策略的推荐姿势。

## 4. 可用指标与命名空间

编辑器代码运行在预置命名空间中，以下内容**无需 import 直接可用**：

- `pd`（pandas）、`np`（numpy）
- 基类：`Strategy` / `PortfolioStrategy` / `OptionStrategy` 及对应 Context 类型
- 指标函数（输入/输出均为 `pd.Series`，与选股表达式共用同一套白名单）：

| 函数 | 说明 |
|---|---|
| `SMA(s, period=20)` / `EMA(s, period=20)` | 简单/指数移动平均 |
| `RSI(s, period=14)` | RSI（横盘时取中性值 50） |
| `MACD(s, fast=12, slow=26, signal=9)` | 返回 **MACD 柱**（DIF−DEA）单序列 |
| `ATR(high, low, close, period=14)` | 平均真实波幅 |
| `HIGHEST(s, period)` / `LOWEST(s, period)` | 滚动最高/最低 |
| `STD(s, period=20)` | 滚动标准差 |
| `REF(s, n=1)` | n 根 bar 之前的值 |
| `ABS(s)` | 绝对值 |

需要更多指标：直接在策略代码里用 pandas 手写，或在 `backend/app/screener/indicators.py` 注册（同时惠及选股表达式）。

## 5. 从编辑器到实盘的完整流程

1. **写**：策略编辑器 → 新建（可从任一内置策略"派生"出可编辑副本）→ 保存（自动编译 + 合成行情试跑，运行期错误挡在保存前）；
2. **回测**：回测中心选择该策略 → 配置参数/标的/区间 → 查看指标、权益曲线、K 线买卖点走查；参数没把握就用**参数扫描**（参数值写成数组，如 `{"fast": [5,10,20]}`，上限 60 组合）；
3. **绑实盘**：策略管理 → 新建配置 → 选择该策略类 → 填监控标的、cron 运行时间（北京时间）、K 线周期（1d/60m/15m/5m）、执行账户 → **模式先选 `signal_only`**（只提醒不下单）观察几天 → 确认信号质量后切 `live`；
4. **观察**：信号日志页看每次触发与风控判定；绩效分析页看按策略归因的实盘盈亏。

## 6. 常见坑

- **未来函数**：`ctx.history(n)` 已保证不含未来数据，但注意不要用 `iloc[-1]` 的当根收盘价做"本根开盘就买入"的假设——成交价是**下一根开盘价**；
- **数据不足**：指标窗口期内 `history` 返回的行数不够时先 `return`，否则指标 NaN 会让比较逻辑静默失效；
- **重复下单**：实盘每次调度都会从头执行 `on_bar`，务必用 `ctx.position()` 判断当前状态再决定动作（内置策略都是"空仓才买、有仓才卖"的写法）；
- **分钟线覆盖范围**：yfinance 分钟线仅近 60 天、akshare 分钟线仅 A股近期——分钟级回测的区间别设太长；
- **params 类型**：JSON 注入的参数可能是字符串/浮点，使用前 `int()`/`float()` 显式转换（内置策略的写法可参考）。
