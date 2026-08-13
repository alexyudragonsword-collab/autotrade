<template>
  <div v-loading="loading">
    <div style="display: flex; gap: 12px; margin-bottom: 16px; align-items: center">
      <el-radio-group v-model="days" @change="load">
        <el-radio-button :value="7">{{ $t('近 7 天') }}</el-radio-button>
        <el-radio-button :value="30">{{ $t('近 30 天') }}</el-radio-button>
        <el-radio-button :value="90">{{ $t('近 90 天') }}</el-radio-button>
        <el-radio-button :value="365">{{ $t('近一年') }}</el-radio-button>
      </el-radio-group>
      <el-button size="small" @click="snapshot">{{ $t('记录净值快照') }}</el-button>
    </div>

    <el-row :gutter="16">
      <el-col :span="6">
        <el-card>
          <div class="stat-label">{{ $t('已实现盈亏') }} ({{ days }}D)</div>
          <div class="stat-num" :style="{ color: (summary.total_realized_pnl || 0) >= 0 ? '#ef4444' : '#10b981' }">
            {{ fmt(summary.total_realized_pnl) }}
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card>
          <div class="stat-label">{{ $t('总手续费') }}</div>
          <div class="stat-num">{{ fmt(summary.total_fees) }}</div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <div class="stat-label">{{ $t('账户净值（快照，每 4 小时自动记录）') }}</div>
          <div ref="equityEl" style="height: 120px" />
        </el-card>
      </el-col>
    </el-row>

    <el-card style="margin-top: 16px">
      <div class="stat-label" style="margin-bottom: 8px">{{ $t('每日已实现盈亏') }}</div>
      <div ref="dailyEl" style="height: 220px" />
    </el-card>

    <el-row :gutter="16" style="margin-top: 16px">
      <el-col :span="8">
        <el-card :header="$t('按策略')">
          <el-table :data="summary.by_strategy" size="small">
            <el-table-column prop="key" :label="$t('策略')" />
            <el-table-column :label="$t('盈亏')" width="110">
              <template #default="{ row }">
                <span :style="{ color: row.realized_pnl >= 0 ? '#ef4444' : '#10b981' }">{{ fmt(row.realized_pnl) }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="$t('胜率')" width="80">
              <template #default="{ row }">{{ row.win_rate != null ? pct(row.win_rate) : '-' }}</template>
            </el-table-column>
            <el-table-column prop="closed_trades" :label="$t('平仓次数')" width="90" />
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card :header="$t('按账户')">
          <el-table :data="summary.by_account" size="small">
            <el-table-column prop="key" :label="$t('账户')" />
            <el-table-column :label="$t('盈亏')" width="110">
              <template #default="{ row }">
                <span :style="{ color: row.realized_pnl >= 0 ? '#ef4444' : '#10b981' }">{{ fmt(row.realized_pnl) }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="fees" :label="$t('手续费')" width="90" />
            <el-table-column prop="fills" :label="$t('成交数')" width="80" />
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card :header="$t('按标的')">
          <el-table :data="summary.by_symbol" size="small" max-height="360">
            <el-table-column prop="key" :label="$t('标的')" />
            <el-table-column :label="$t('盈亏')" width="110">
              <template #default="{ row }">
                <span :style="{ color: row.realized_pnl >= 0 ? '#ef4444' : '#10b981' }">{{ fmt(row.realized_pnl) }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="$t('胜率')" width="80">
              <template #default="{ row }">{{ row.win_rate != null ? pct(row.win_rate) : '-' }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import * as echarts from 'echarts'
import client from '../api/client'
import { tr } from '../i18n'
import { fmt, pct } from '../utils'

const days = ref(30)
const summary = ref({ by_strategy: [], by_account: [], by_symbol: [] })
const loading = ref(true)
const equityEl = ref(null)
const dailyEl = ref(null)
let equityChart = null
let dailyChart = null

async function load() {
  loading.value = true
  try {
    summary.value = await client.get('/api/performance/summary', { params: { days: days.value } })
    const curves = await client.get('/api/performance/equity', { params: { days: Math.max(days.value, 90) } })
    await nextTick()
    renderEquity(curves)
    renderDaily(summary.value.daily_pnl || [])
  } finally {
    loading.value = false
  }
}

function renderEquity(curves) {
  if (!equityChart) equityChart = echarts.init(equityEl.value)
  const series = Object.entries(curves).map(([name, points]) => ({
    name, type: 'line', showSymbol: false,
    data: points.map((p) => p),
  }))
  equityChart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { show: series.length > 1, top: 0 },
    grid: { top: series.length > 1 ? 24 : 8, bottom: 20, left: 60, right: 10 },
    xAxis: { type: 'time' },
    yAxis: { type: 'value', scale: true },
    series,
  }, true)
  equityChart.resize()
}

function renderDaily(daily) {
  if (!dailyChart) dailyChart = echarts.init(dailyEl.value)
  dailyChart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { top: 10, bottom: 24, left: 60, right: 10 },
    xAxis: { type: 'category', data: daily.map((d) => d[0]) },
    yAxis: { type: 'value' },
    series: [{
      type: 'bar',
      data: daily.map((d) => ({
        value: d[1],
        itemStyle: { color: d[1] >= 0 ? '#ef4444' : '#10b981' },
      })),
    }],
  }, true)
  dailyChart.resize()
}

async function snapshot() {
  const data = await client.post('/api/performance/snapshot')
  ElMessage.success(`${tr('已记录净值快照')} (${data.recorded})`)
  load()
}

onMounted(load)
onUnmounted(() => {
  equityChart?.dispose()
  dailyChart?.dispose()
})
</script>

<style scoped>
.stat-label { color: #6b7280; font-size: 13px; }
.stat-num { font-size: 28px; font-weight: 700; margin-top: 4px; }
</style>
