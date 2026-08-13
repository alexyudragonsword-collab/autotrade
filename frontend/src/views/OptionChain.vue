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
                 @change="loadChain">
        <el-option v-for="e in expirations" :key="e" :label="fmtDate(e)" :value="e" />
      </el-select>
      <el-switch v-if="expiry" v-model="withQuotes" :active-text="$t('含报价')" @change="loadChain" />
      <span v-if="chain" style="color: #6b7280; font-size: 13px; margin-left: auto">
        {{ $t('正股') }} {{ chain.underlying_price != null ? chain.underlying_price : '-' }} · ×{{ chain.multiplier }}
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
        <el-table-column label="" width="80" align="center">
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
        <el-table-column label="" width="80" align="center">
          <template #default="{ row }">
            <el-button v-if="row.put" link type="success" size="small" @click="trade(row.put)">{{ $t('交易') }}</el-button>
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
import { computed, onMounted, ref } from 'vue'
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

async function loadChain() {
  if (!expiry.value) return
  error.value = ''
  loadingChain.value = true
  try {
    chain.value = await client.get('/api/options/chain', {
      params: { broker: broker.value, underlying: underlying.value, expiry: expiry.value,
                with_quotes: withQuotes.value, strikes_around: 20 },
    })
  } catch (e) {
    chain.value = null
    error.value = e.response?.data?.detail || String(e)
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
</script>

<style>
.atm-row { background: #eff6ff !important; }
</style>
