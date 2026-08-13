"""自包含 HTML 回测报告（无外部依赖，可离线分享）。"""

import html

from app.db.models import BacktestRun


def _svg_lines(series_list: list[tuple[list[float], str, float]], width=900, height=260) -> str:
    """多条折线的内联 SVG。series_list: [(values, color, stroke_width)]"""
    all_vals = [v for values, _, _ in series_list for v in values if v is not None]
    if not all_vals:
        return ""
    vmin, vmax = min(all_vals), max(all_vals)
    span = (vmax - vmin) or 1.0
    parts = [f'<svg viewBox="0 0 {width} {height}" style="width:100%;background:#f9fafb;'
             f'border-radius:8px">']
    for values, color, sw in series_list:
        n = len(values)
        if n < 2:
            continue
        points = " ".join(
            f"{i * width / (n - 1):.1f},{height - 10 - (v - vmin) / span * (height - 20):.1f}"
            for i, v in enumerate(values) if v is not None)
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="{sw}" '
                     f'points="{points}"/>')
    parts.append("</svg>")
    return "".join(parts)


def render_report(run: BacktestRun) -> str:
    m = run.metrics or {}
    curve = run.equity_curve or []
    equity = [p[1] for p in curve]
    benchmark = [p[2] for p in curve if len(p) > 2]
    series = [(equity, "#2563eb", 2.0)]
    if benchmark and len(benchmark) == len(equity):
        series.append((benchmark, "#9ca3af", 1.2))

    def pct(x):
        return f"{x * 100:.2f}%" if isinstance(x, (int, float)) else "-"

    metric_cells = "".join(
        f'<div class="m"><div class="k">{k}</div><div class="v">{v}</div></div>'
        for k, v in [
            ("总收益", pct(m.get("total_return"))), ("年化收益", pct(m.get("annual_return"))),
            ("夏普比率", m.get("sharpe", "-")), ("最大回撤", pct(m.get("max_drawdown"))),
            ("胜率", pct(m.get("win_rate"))), ("交易次数", m.get("trade_count", "-")),
            ("基准收益", pct(m.get("benchmark_return")) if "benchmark_return" in m else "-"),
            ("超额收益α", pct(m.get("alpha")) if "alpha" in m else "-"),
            ("期末权益", f'{m.get("final_equity", 0):,.0f}'),
        ])

    monthly = "".join(
        f'<span class="mo" style="background:{"#fee2e2" if r["ret"] >= 0 else "#d1fae5"};'
        f'color:{"#dc2626" if r["ret"] >= 0 else "#059669"}">{r["month"]}<br>'
        f'<b>{r["ret"] * 100:.1f}%</b></span>'
        for r in m.get("monthly_returns", []))

    trade_rows = "".join(
        f'<tr><td>{html.escape(str(t.get("date", "")))}</td>'
        f'<td>{html.escape(str(t.get("symbol", "")))}</td>'
        f'<td style="color:{"#dc2626" if t.get("side") == "buy" else "#059669"}">'
        f'{"买入" if t.get("side") == "buy" else "卖出"}</td>'
        f'<td>{t.get("qty", "")}</td><td>{t.get("price", "")}</td>'
        f'<td>{t.get("fee", "")}</td><td>{t.get("pnl", "") if t.get("pnl") is not None else ""}</td></tr>'
        for t in (run.trades or []))

    symbols = html.escape(", ".join(run.symbols or []))
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>回测报告 #{run.id} - {html.escape(run.strategy_class)}</title>
<style>
body{{font-family:'Helvetica Neue','PingFang SC','Microsoft YaHei',sans-serif;margin:24px auto;
max-width:960px;color:#111827}}
h1{{font-size:22px}} h2{{font-size:16px;margin-top:28px;border-bottom:1px solid #e5e7eb;
padding-bottom:6px}}
.meta{{color:#6b7280;font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin:16px 0}}
.m{{background:#f9fafb;border-radius:8px;padding:10px 14px}}
.k{{color:#6b7280;font-size:12px}} .v{{font-size:18px;font-weight:700;margin-top:2px}}
.mo{{display:inline-block;border-radius:6px;padding:4px 10px;margin:3px;font-size:12px;
text-align:center}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:6px 8px;border-bottom:1px solid #f3f4f6;text-align:left}}
th{{background:#f9fafb;color:#6b7280}}
.legend{{font-size:12px;color:#6b7280}}
</style></head><body>
<h1>回测报告 #{run.id} — {html.escape(run.strategy_class)}</h1>
<div class="meta">标的：{symbols}　|　区间：{run.start_date} ~ {run.end_date}（{run.timeframe}）
　|　初始资金：{run.initial_cash:,.0f}　|　参数：{html.escape(str(run.params))}</div>
<div class="grid">{metric_cells}</div>
<h2>权益曲线 <span class="legend">（蓝=策略{"，灰=买入持有基准" if benchmark else ""}）</span></h2>
{_svg_lines(series)}
<h2>月度收益</h2><div>{monthly or "（无数据）"}</div>
<h2>交易明细（{len(run.trades or [])} 笔）</h2>
<table><thead><tr><th>日期</th><th>标的</th><th>方向</th><th>数量</th><th>价格</th>
<th>费用</th><th>已实现盈亏</th></tr></thead><tbody>{trade_rows}</tbody></table>
<div class="meta" style="margin-top:24px">AutoTrade 生成 · 仅供研究参考，不构成投资建议</div>
</body></html>"""
