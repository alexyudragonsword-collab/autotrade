<template>
  <el-card>
    <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 12px; flex-wrap: wrap">
      <el-select v-model="broker" :placeholder="$t('选择账户')" style="width: 180px">
        <el-option v-for="a in brokerAccounts" :key="a.name" :label="`${a.name}（${a.type}）`" :value="a.name" />
      </el-select>
      <el-input v-model="underlying" :placeholder="$t('标的：US.AAPL / HK.00700')" style="width: 220px"
                @keyup.enter="loadExpirations" />
      <el-button type="primary" :loading="loadingExp" @click="loadExpirations">{{ $t('加载到期日') }}</el-button>
      <el-select v-if="expirations.length" v-model="expiry" :placeholder="$t('到期日')" style="width: 160px"
                 @change="() => loadChain()">
        <el-option v-for="e in expirations" :key="e" :label="fmtDate(e)" :value="e" />
      </el-select>
      <el-switch v-if="expiry" v-model="withQuotes" :active-text="$t('含报价')" @change="onQuotesToggle" />
      <el-switch v-if="expiry && withQuotes" v-model="autoRefresh" :active-text="$t('自动刷新')" @change="setupAutoRefresh" />
      <el-select v-if="autoRefresh && withQuotes" v-model="refreshSec" style="width: 100px" @change="setupAutoRefresh">
        <el-option v-for="s in [5, 10, 30, 60]" :key="s" :label="`${s}s`" :value="s" />
      </el-select>
      <span v-if="chain" style="color: #6b7280; font-size: 13px; margin-left: auto">
        {{ $t('正股') }} {{ chain.underlying_price != null ? chain.underlying_price : '-' }} · ×{{ chain.multiplier }}
        <span v-if="lastUpdated" style="margin-left: 8px">· {{ $t('更新于') }} {{ lastUpdated }}</span>
      </span>
    </div>

    <el-alert v-if="error" type="warning" :title="error" :closable="false" show-icon style="margin-bottom: 12px" />

    <el-table v-if="chain" :data="chain.rows" v-loading="loadingChain" size="small"
              max-height="600" :row-class-name="rowClass">
      <el-table-column label="Call" align="center">
        <el-table-column :label="$t('买价')" width="80" align="right">
          <template #default="{ row }">{{ n(row.call?.bid) }}</template>
        </el-table-column>
        <el-table-column :label="$t('卖价')" width="80" align="right">
          <template #default="{ row }">{{ n(row.call?.ask) }}</template>
        </el-table-column>
        <el-table-column :label="$t('最新')" width="80" align="right">
          <template #default="{ row }">{{ n(row.call?.last) }}</template>
        </el-table-column>
        <el-table-column :label="$t('价差')" width="72" align="right">
          <template #default="{ row }">
            <span :style="{ color: spreadColor(row.call) }">{{ spreadText(row.call) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="" width="72" align="center">
          <template #default="{ row }">
            <el-button v-if="row.call" link type="danger" size="small" @click="trade(row.call)">{{ $t('交易') }}</el-button>
          </template>
        </el-table-column>
      </el-table-column>
      <el-table-column prop="strike" :label="$t('行权价')" width="100" align="center">
        <template #default="{ row }">
          <b :style="{ color: isAtm(row.strike) ? '#2563eb' : '' }">{{ row.strike }}</b>
        </template>
      </el-table-column>
      <el-table-column label="Put" align="center">
        <el-table-column label="" width="72" align="center">
          <template #default="{ row }">
            <el-button v-if="row.put" link type="success" size="small" @click="trade(row.put)">{{ $t('交易') }}</el-button>
          </template>
        </el-table-column>
        <el-table-column :label="$t('价差')" width="72" align="right">
          <template #default="{ row }">
            <span :style="{ color: spreadColor(row.put) }">{{ spreadText(row.put) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="$t('买价')" width="80" align="right">
          <template #default="{ row }">{{ n(row.put?.bid) }}</template>
        </el-table-column>
        <el-table-column :label="$t('卖价')" width="80" align="right">
          <template #default="{ row }">{{ n(row.put?.ask) }}</template>
        </el-table-column>
        <el-table-column :label="$t('最新')" width="80" align="right">
          <template #default="{ row }">{{ n(row.put?.last) }}</template>
        </el-table-column>
      </el-table-column>
    </el-table>
    <el-empty v-else-if="!error" :description="$t('选择账户与标的后加载期权链（需要 IBKR 或富途账户在线）')" />

    <el-alert type="info" :closable="false" show-icon style="margin-top: 12px"
              :title="$t('点击「交易」预填下单框；卖出无持仓即为卖方开仓，默认档要求备兑（Call）或现金担保（Put），裸卖需在风控设置显式开启。期权交易需在风控设置中先启用。')" />
  </el-card>

  <manual-order-dialog v-model="dialogOpen" :prefill="prefill" />
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import client from '../api/client'
import { tr } from '../i18n'
import ManualOrderDialog from '../components/ManualOrderDialog.vue'

const accounts = ref([])
const broker = ref('')
const underlying = ref('')
const expirations = ref([])
const expiry = ref('')
const withQuotes = ref(true)
const chain = ref(null)
const error = ref('')
const loadingExp = ref(false)
const loadingChain = ref(false)
const dialogOpen = ref(false)
const prefill = ref(null)
const autoRefresh = ref(false)
const refreshSec = ref(10)
const lastUpdated = ref('')
let refreshTimer = null

/** 买卖价差占中间价的比例——宽价差意味着流动性差，滑点会吃掉权利金。 */
function spreadRatio(c) {
  if (!c || c.bid == null || c.ask == null) return null
  const mid = (c.bid + c.ask) / 2
  if (!mid) return null
  return (c.ask - c.bid) / mid
}

function spreadText(c) {
  const r = spreadRatio(c)
  return r == null ? '-' : (r * 100).toFixed(1) + '%'
}

function spreadColor(c) {
  const r = spreadRatio(c)
  if (r == null) return ''
  if (r > 0.15) return '#ef4444'
  if (r > 0.05) return '#e6a23c'
  return '#22c55e'
}

function setupAutoRefresh() {
  clearInterval(refreshTimer)
  refreshTimer = null
  // 仅在已选到期日且要报价时轮询——无报价的链是静态数据，刷了也不会变
  if (autoRefresh.value && withQuotes.value && expiry.value) {
    refreshTimer = setInterval(() => loadChain(true), refreshSec.value * 1000)
  }
}

function onQuotesToggle() {
  setupAutoRefresh()
  loadChain()
}

const brokerAccounts = computed(() =>
  accounts.value.filter((a) => ['futu', 'ibkr'].includes(a.type) && a.connected))

function n(v) { return v != null ? v : '-' }
function fmtDate(e) { return `${e.slice(0, 4)}-${e.slice(4, 6)}-${e.slice(6, 8)}` }

function isAtm(strike) {
  const p = chain.value?.underlying_price
  if (p == null || !chain.value?.rows?.length) return false
  const nearest = chain.value.rows.reduce((a, b) =>
    Math.abs(b.strike - p) < Math.abs(a.strike - p) ? b : a)
  return nearest.strike === strike
}

function rowClass({ row }) { return isAtm(row.strike) ? 'atm-row' : '' }

async function loadExpirations() {
  if (!broker.value || !underlying.value) return
  error.value = ''
  expirations.value = []
  expiry.value = ''
  chain.value = null
  lastUpdated.value = ''
  setupAutoRefresh()  // expiry 已清空，等于停表
  loadingExp.value = true
  try {
    const data = await client.get('/api/options/expirations',
      { params: { broker: broker.value, underlying: underlying.value } })
    expirations.value = data.expirations
    if (!data.expirations.length) error.value = tr('该标的没有可交易期权')
  } catch (e) {
    error.value = e.response?.data?.detail || String(e)
  } finally {
    loadingExp.value = false
  }
}

async function loadChain(silent = false) {
  if (!expiry.value) return
  error.value = ''
  if (!silent) loadingChain.value = true
  try {
    chain.value = await client.get('/api/options/chain', {
      params: { broker: broker.value, underlying: underlying.value, expiry: expiry.value,
                with_quotes: withQuotes.value, strikes_around: 20 },
    })
    lastUpdated.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch (e) {
    error.value = e.response?.data?.detail || String(e)
    if (silent) {
      // 后台刷新失败：保留上一次的链数据（标记为过期），并停掉轮询，
      // 不对已经出问题的网关每几秒重试一次
      autoRefresh.value = false
      setupAutoRefresh()
    } else {
      chain.value = null
    }
  } finally {
    loadingChain.value = false
  }
}

function trade(contract) {
  const mid = contract.bid != null && contract.ask != null
    ? +(((contract.bid + contract.ask) / 2).toFixed(3))
    : contract.last
  prefill.value = {
    broker: broker.value, symbol: contract.symbol, side: 'buy',
    order_type: mid != null ? 'limit' : 'market', qty: 1, limit_price: mid,
  }
  dialogOpen.value = true
}

onMounted(async () => {
  accounts.value = await client.get('/api/broker-accounts')
  if (brokerAccounts.value.length) broker.value = brokerAccounts.value[0].name
})

onUnmounted(() => clearInterval(refreshTimer))
</script>

<style>
.atm-row { background: #eff6ff !important; }
</style>
