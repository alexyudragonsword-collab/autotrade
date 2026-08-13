<template>
  <el-row :gutter="16">
    <el-col :span="7">
      <el-card header="发起回测">
        <el-form :model="form" label-width="90px" label-position="top">
          <el-form-item label="策略">
            <el-select v-model="form.strategy_class" @change="onStrategyChange">
              <el-option v-for="b in builtin" :key="b.class_name" :label="b.class_name" :value="b.class_name" />
            </el-select>
            <div style="color: #9ca3af; font-size: 12px">{{ currentDoc }}</div>
          </el-form-item>
          <el-form-item label="参数(JSON)">
            <el-input v-model="paramsText" type="textarea" :rows="2" />
            <el-checkbox v-model="scanMode" style="margin-top: 4px">参数扫描（值写成数组，如 {"fast":[5,10,20],"slow":[30,60]}）</el-checkbox>
          </el-form-item>
          <el-form-item label="市场">
            <el-select v-model="form.market">
              <el-option label="美股 (US)" value="US" />
              <el-option label="A股 (CN)" value="CN" />
              <el-option label="港股 (HK)" value="HK" />
            </el-select>
          </el-form-item>
          <el-form-item label="标的（逗号分隔）">
            <el-input v-model="symbolsText" placeholder="AAPL,MSFT 或 600519" />
          </el-form-item>
          <el-form-item label="日期区间">
            <el-date-picker v-model="dateRange" type="daterange" value-format="YYYY-MM-DD" style="width: 100%" />
          </el-form-item>
          <el-form-item label="初始资金">
            <el-input-number v-model="form.initial_cash" :min="1000" :step="10000" style="width: 100%" />
          </el-form-item>
          <el-button type="primary" style="width: 100%" :loading="submitting" @click="submit">开始回测</el-button>
        </el-form>
      </el-card>

      <el-card header="历史回测" style="margin-top: 16px">
        <el-table :data="runs" size="small" highlight-current-row @current-change="showRun">
          <el-table-column prop="id" label="#" width="50" />
          <el-table-column prop="strategy_class" label="策略" />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-progress v-if="row.status === 'running'" :percentage="Math.round(row.progress * 100)" :stroke-width="6" />
              <el-tag v-else :type="{ done: 'success', failed: 'danger', queued: 'info' }[row.status]" size="small">
                {{ { done: '完成', failed: '失败', queued: '排队' }[row.status] }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </el-col>

    <el-col :span="17">
      <el-card v-if="scanGroup" :header="`参数扫描对比 — 已完成 ${scanGroup.finished}/${scanGroup.total}（按夏普降序）`" style="margin-bottom: 16px">
        <el-table :data="scanGroup.items" size="small" max-height="380" highlight-current-row
                  @current-change="(row) => row && showRun(row)">
          <el-table-column prop="id" label="#" width="60" />
          <el-table-column label="参数"><template #default="{ row }">{{ JSON.stringify(row.params) }}</template></el-table-column>
          <el-table-column label="夏普" width="80"><template #default="{ row }">{{ row.metrics?.sharpe ?? '…' }}</template></el-table-column>
          <el-table-column label="年化" width="90"><template #default="{ row }">{{ row.metrics ? pct(row.metrics.annual_return) : '…' }}</template></el-table-column>
          <el-table-column label="回撤" width="90"><template #default="{ row }">{{ row.metrics ? pct(row.metrics.max_drawdown) : '…' }}</template></el-table-column>
          <el-table-column label="胜率" width="80"><template #default="{ row }">{{ row.metrics ? pct(row.metrics.win_rate) : '…' }}</template></el-table-column>
        </el-table>
      </el-card>
      <el-card v-if="detail" :header="`回测 #${detail.id} — ${detail.strategy_class} ${JSON.stringify(detail.params)}`">
        <el-alert v-if="detail.error_msg" type="error" :title="detail.error_msg" :closable="false" style="margin-bottom: 12px" />
        <template v-if="detail.metrics">
          <el-row :gutter="12">
            <el-col v-for="m in metricCards" :key="m.label" :span="6">
              <div class="metric">
                <div class="metric-label">{{ m.label }}</div>
                <div class="metric-value" :style="{ color: m.color }">{{ m.value }}</div>
              </div>
            </el-col>
          </el-row>
          <div ref="chartEl" style="height: 360px; margin-top: 16px" />
          <template v-if="detail.metrics?.monthly_returns?.length">
            <el-divider>月度收益</el-divider>
            <div style="display: flex; flex-wrap: wrap; gap: 6px">
              <div v-for="m in detail.metrics.monthly_returns" :key="m.month" class="month-cell"
                   :style="{ background: m.ret >= 0 ? 'rgba(239,68,68,0.12)' : 'rgba(16,185,129,0.12)',
                             color: m.ret >= 0 ? '#dc2626' : '#059669' }">
                <div style="font-size: 11px; color: #6b7280">{{ m.month }}</div>
                <div style="font-weight: 600">{{ pct(m.ret) }}</div>
              </div>
            </div>
          </template>
          <el-divider>交易明细（{{ (detail.trades || []).length }} 笔）</el-divider>
          <el-table :data="detail.trades" size="small" max-height="300">
            <el-table-column prop="date" label="日期" width="110" />
            <el-table-column prop="symbol" label="标的" width="100" />
            <el-table-column prop="side" label="方向" width="70">
              <template #default="{ row }">
                <span :style="{ color: row.side === 'buy' ? '#ef4444' : '#10b981' }">{{ row.side === 'buy' ? '买入' : '卖出' }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="qty" label="数量" width="90" />
            <el-table-column prop="price" label="价格" width="100" />
            <el-table-column prop="fee" label="费用" width="90" />
            <el-table-column prop="pnl" label="已实现盈亏">
              <template #default="{ row }">
                <span v-if="row.pnl != null" :style="{ color: row.pnl >= 0 ? '#ef4444' : '#10b981' }">{{ row.pnl }}</span>
              </template>
            </el-table-column>
          </el-table>
        </template>
        <el-empty v-else-if="detail.status !== 'failed'" description="回测运行中…" />
      </el-card>
      <el-empty v-else-if="!scanGroup" description="发起回测或从左侧选择历史记录" />
    </el-col>
  </el-row>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import * as echarts from 'echarts'
import client from '../api/client'
import { pct, fmt } from '../utils'

const builtin = ref([])
const runs = ref([])
const detail = ref(null)
const chartEl = ref(null)
const submitting = ref(false)
const paramsText = ref('{}')
const symbolsText = ref('AAPL')
const dateRange = ref(['2023-01-01', '2026-01-01'])
const form = ref({ strategy_class: 'SmaCross', market: 'US', initial_cash: 100000 })
const scanMode = ref(false)
const scanGroup = ref(null)
let chart = null
let timer = null
let scanTimer = null

const currentDoc = computed(() =>
  builtin.value.find((b) => b.class_name === form.value.strategy_class)?.doc || '')

const metricCards = computed(() => {
  const m = detail.value?.metrics
  if (!m) return []
  const cards = [
    { label: '总收益', value: pct(m.total_return), color: m.total_return >= 0 ? '#ef4444' : '#10b981' },
    { label: '年化收益', value: pct(m.annual_return), color: m.annual_return >= 0 ? '#ef4444' : '#10b981' },
    { label: '夏普比率', value: m.sharpe },
    { label: '最大回撤', value: pct(m.max_drawdown), color: '#10b981' },
    { label: '胜率', value: pct(m.win_rate) },
    { label: '交易次数', value: m.trade_count },
    { label: '期末权益', value: fmt(m.final_equity) },
  ]
  if (m.benchmark_return != null) {
    cards.push({ label: '基准(买入持有)', value: pct(m.benchmark_return) })
    cards.push({ label: '超额收益 α', value: pct(m.alpha), color: m.alpha >= 0 ? '#ef4444' : '#10b981' })
  }
  return cards
})

function onStrategyChange(name) {
  const b = builtin.value.find((x) => x.class_name === name)
  paramsText.value = JSON.stringify(b?.params || {})
}

async function submit() {
  let params
  try {
    params = JSON.parse(paramsText.value || '{}')
  } catch {
    ElMessage.error('参数不是合法 JSON')
    return
  }
  submitting.value = true
  try {
    const common = {
      symbols: symbolsText.value.split(/[,，\s]+/).filter(Boolean),
      start_date: dateRange.value[0],
      end_date: dateRange.value[1],
    }
    if (scanMode.value) {
      const data = await client.post('/api/backtests/scan', { ...form.value, param_grid: params, ...common })
      ElMessage.success(`参数扫描已提交（${data.count} 组）`)
      pollScan(data.group_id)
    } else {
      const run = await client.post('/api/backtests', { ...form.value, params, ...common })
      ElMessage.success('回测已提交')
      pollRun(run.id)
    }
    await loadRuns()
  } finally {
    submitting.value = false
  }
}

function pollScan(groupId) {
  clearInterval(scanTimer)
  const tick = async () => {
    scanGroup.value = await client.get(`/api/backtests/groups/${groupId}`)
    if (scanGroup.value.finished >= scanGroup.value.total) clearInterval(scanTimer)
  }
  tick()
  scanTimer = setInterval(tick, 2000)
}

async function loadRuns() {
  const data = await client.get('/api/backtests', { params: { size: 15 } })
  runs.value = data.items
}

function pollRun(id) {
  clearInterval(timer)
  timer = setInterval(async () => {
    const run = await client.get(`/api/backtests/${id}`)
    const idx = runs.value.findIndex((r) => r.id === id)
    if (idx >= 0) runs.value[idx] = { ...runs.value[idx], status: run.status, progress: run.progress }
    if (run.status === 'done' || run.status === 'failed') {
      clearInterval(timer)
      render(run)
    }
  }, 1500)
}

async function showRun(row) {
  if (!row) return
  const run = await client.get(`/api/backtests/${row.id}`)
  if (run.status === 'running' || run.status === 'queued') pollRun(row.id)
  render(run)
}

async function render(run) {
  detail.value = run
  if (!run.equity_curve?.length) return
  await nextTick()
  if (!chartEl.value) return
  if (!chart) chart = echarts.init(chartEl.value)
  const dates = run.equity_curve.map((p) => p[0])
  const equity = run.equity_curve.map((p) => p[1])
  const benchmark = run.equity_curve[0]?.length > 2 ? run.equity_curve.map((p) => p[2]) : null
  let peak = -Infinity
  const drawdown = equity.map((e) => {
    peak = Math.max(peak, e)
    return +(((e - peak) / peak) * 100).toFixed(2)
  })
  const series = [
    { name: '策略权益', type: 'line', data: equity, showSymbol: false, lineStyle: { width: 2 } },
  ]
  if (benchmark) {
    series.push({ name: '基准(买入持有)', type: 'line', data: benchmark, showSymbol: false,
                  lineStyle: { width: 1.5, type: 'dashed', color: '#9ca3af' } })
  }
  series.push({ name: '回撤%', type: 'line', data: drawdown, showSymbol: false, xAxisIndex: 1, yAxisIndex: 1,
                areaStyle: { color: 'rgba(16,185,129,0.25)' }, lineStyle: { color: '#10b981' } })
  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: series.map((s) => s.name) },
    grid: [{ top: 40, height: '55%' }, { top: '72%', height: '18%' }],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0 },
      { type: 'category', data: dates, gridIndex: 1, show: false },
    ],
    yAxis: [
      { type: 'value', scale: true, gridIndex: 0 },
      { type: 'value', gridIndex: 1, max: 0 },
    ],
    series,
  }, true)
  chart.resize()
}

onMounted(async () => {
  builtin.value = await client.get('/api/strategies/builtin')
  onStrategyChange(form.value.strategy_class)
  loadRuns()
})
onUnmounted(() => {
  clearInterval(timer)
  clearInterval(scanTimer)
  chart?.dispose()
})
</script>

<style scoped>
.metric { background: #f9fafb; border-radius: 8px; padding: 10px 14px; margin-bottom: 10px; }
.metric-label { color: #6b7280; font-size: 12px; }
.metric-value { font-size: 20px; font-weight: 700; }
.month-cell { border-radius: 6px; padding: 6px 10px; min-width: 76px; text-align: center; }
</style>
