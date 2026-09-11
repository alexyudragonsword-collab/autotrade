<template>
  <el-card>
    <div style="margin-bottom: 12px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center">
      <el-select v-model="status" :placeholder="$t('全部状态')" clearable style="width: 150px" @change="load(1)">
        <el-option v-for="(v, k) in statusOptions" :key="k" :label="$t(v)" :value="k" />
      </el-select>
      <el-select v-model="strategy" :placeholder="$t('全部策略')" clearable filterable style="width: 150px" @change="load(1)">
        <el-option v-for="s in strategyOptions" :key="s" :label="s" :value="s" />
      </el-select>
      <el-select v-model="source" :placeholder="$t('全部来源')" clearable style="width: 140px" @change="load(1)">
        <el-option v-for="(v, k) in sourceOptions" :key="k" :label="$t(v)" :value="k" />
      </el-select>
      <el-input v-model="symbol" :placeholder="$t('标的（支持模糊）')" clearable style="width: 170px"
                @keyup.enter="load(1)" @clear="load(1)" />
      <el-date-picker v-model="dateRange" type="daterange" :start-placeholder="$t('开始日期')"
                      :end-placeholder="$t('结束日期')" style="width: 240px" @change="load(1)" />
      <el-button @click="load(1)">{{ $t('查询') }}</el-button>
      <el-button @click="reset">{{ $t('重置') }}</el-button>
      <el-button type="primary" plain :loading="exporting" @click="exportCsv">{{ $t('导出 CSV') }}</el-button>
    </div>
    <el-table :data="items" v-loading="loading">
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="created_at" :label="$t('时间')" width="165"><template #default="{ row }">{{ ts(row.created_at) }}</template></el-table-column>
      <el-table-column prop="source" :label="$t('来源')" width="110" />
      <el-table-column prop="strategy_name" :label="$t('策略')" width="120" />
      <el-table-column prop="symbol" :label="$t('标的')" width="110" />
      <el-table-column prop="action" :label="$t('动作')" width="70" />
      <el-table-column prop="quantity" :label="$t('数量')" width="80" />
      <el-table-column prop="price" :label="$t('价格')" width="90" />
      <el-table-column prop="status" :label="$t('状态')" width="110">
        <template #default="{ row }"><signal-status :status="row.status" /></template>
      </el-table-column>
      <el-table-column prop="reject_reason" :label="$t('说明')" show-overflow-tooltip />
      <el-table-column label="" width="90">
        <template #default="{ row }">
          <el-button link type="primary" @click="showDetail(row.id)">{{ $t('详情') }}</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination style="margin-top: 12px" layout="total, prev, pager, next" :total="total"
                   :page-size="20" :current-page="page" @current-change="load" />

    <el-drawer v-model="drawer" :title="$t('信号详情')" size="45%">
      <pre style="background: #f9fafb; padding: 12px; border-radius: 6px; overflow: auto">{{ JSON.stringify(detail, null, 2) }}</pre>
    </el-drawer>
  </el-card>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import client from '../api/client'
import SignalStatus from '../components/SignalStatus.vue'
import { downloadFile, rangeParams, ts } from '../utils'
import { tr } from '../i18n'

const items = ref([])
const total = ref(0)
const page = ref(1)
const status = ref('')
const strategy = ref('')
const source = ref('')
const symbol = ref('')
const dateRange = ref(null)
const strategyOptions = ref([])
const loading = ref(false)
const exporting = ref(false)
const drawer = ref(false)
const detail = ref({})

const statusOptions = {
  received: '已接收', rejected_risk: '风控拦截', rejected: '已拒绝',
  routed: '已下单', executed: '已执行', failed: '失败',
}
const sourceOptions = {
  tradingview: 'TradingView', strategy: '本地策略', manual: '手动',
  risk_guard: '持仓守护', expiry_guard: '到期守护',
}

/** 当前筛选条件 → 查询参数（列表与导出共用，保证导出的就是看到的） */
function filterParams() {
  return {
    status: status.value || undefined,
    strategy: strategy.value || undefined,
    source: source.value || undefined,
    symbol: symbol.value || undefined,
    ...rangeParams(dateRange.value),
  }
}

async function load(p = 1) {
  page.value = p
  loading.value = true
  try {
    const data = await client.get('/api/signals', { params: { page: p, size: 20, ...filterParams() } })
    items.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

function reset() {
  status.value = ''
  strategy.value = ''
  source.value = ''
  symbol.value = ''
  dateRange.value = null
  load(1)
}

async function exportCsv() {
  exporting.value = true
  try {
    await downloadFile('/api/signals/export.csv', 'signals.csv', filterParams())
  } catch (e) {
    ElMessage.error(`${tr('导出失败')}: ${e.message}`)
  } finally {
    exporting.value = false
  }
}

async function showDetail(id) {
  detail.value = await client.get(`/api/signals/${id}`)
  drawer.value = true
}

onMounted(async () => {
  load()
  try {
    const strategies = await client.get('/api/strategies')
    strategyOptions.value = strategies.map((s) => s.name)
  } catch { /* 选项加载失败不影响列表 */ }
})
</script>
