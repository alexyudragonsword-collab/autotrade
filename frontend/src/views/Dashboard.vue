<template>
  <div v-loading="loading">
    <el-row :gutter="16">
      <el-col :span="6">
        <el-card>
          <div class="stat-label">交易总开关</div>
          <div style="display: flex; align-items: center; gap: 12px; margin-top: 8px">
            <el-switch v-model="tradingEnabled" size="large" active-text="允许交易"
                       inactive-text="已停止" @change="toggleKillSwitch" />
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card><div class="stat-label">今日信号</div><div class="stat-num">{{ summary.signals_today }}</div></el-card>
      </el-col>
      <el-col :span="6">
        <el-card><div class="stat-label">今日订单</div><div class="stat-num">{{ summary.orders_today }}</div></el-card>
      </el-col>
      <el-col :span="6">
        <el-card><div class="stat-label">今日风控拦截</div>
          <div class="stat-num" :style="{ color: summary.blocked_today ? '#ef4444' : '' }">
            {{ summary.blocked_today }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" style="margin-top: 16px">
      <el-col :span="8">
        <el-card header="券商连接状态">
          <div v-for="(info, name) in summary.brokers" :key="name" class="broker-row">
            <span>
              <el-tag :type="info.connected ? 'success' : 'danger'" size="small" effect="dark" round>
                {{ info.connected ? '在线' : '离线' }}
              </el-tag>
              <b style="margin-left: 8px">{{ name }}</b>
              <span style="color: #9ca3af; font-size: 12px; margin-left: 6px">{{ (info.markets || []).join('/') }}</span>
            </span>
            <span v-if="summary.accounts?.[name]" style="font-size: 13px">
              净值 {{ fmt(summary.accounts[name].net_value) }}
            </span>
          </div>
        </el-card>
        <el-card style="margin-top: 16px">
          <template #header>自选关注（{{ watchlist.length }}）</template>
          <el-table :data="watchlist" size="small" max-height="220" v-if="watchlist.length">
            <el-table-column prop="symbol" label="代码" />
            <el-table-column prop="last_close" label="最近收盘"><template #default="{ row }">{{ fmt(row.last_close) }}</template></el-table-column>
            <el-table-column label="" width="60">
              <template #default="{ row }">
                <el-button link type="danger" size="small" @click="removeWatch(row.symbol)">移除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-else description="选股器结果可一键加自选" :image-size="48" />
        </el-card>
      </el-col>
      <el-col :span="16">
        <el-card header="最近信号">
          <el-table :data="summary.recent_signals" size="small">
            <el-table-column prop="created_at" label="时间" width="165"><template #default="{ row }">{{ ts(row.created_at) }}</template></el-table-column>
            <el-table-column prop="strategy_name" label="策略" width="110" />
            <el-table-column prop="symbol" label="标的" width="110" />
            <el-table-column prop="action" label="动作" width="70" />
            <el-table-column prop="status" label="状态" width="110">
              <template #default="{ row }"><signal-status :status="row.status" /></template>
            </el-table-column>
            <el-table-column prop="reject_reason" label="说明" show-overflow-tooltip />
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <el-card header="最近订单" style="margin-top: 16px">
      <el-table :data="summary.recent_orders" size="small">
        <el-table-column prop="created_at" label="时间" width="165"><template #default="{ row }">{{ ts(row.created_at) }}</template></el-table-column>
        <el-table-column prop="broker" label="券商" width="90" />
        <el-table-column prop="symbol" label="标的" width="110" />
        <el-table-column prop="side" label="方向" width="70">
          <template #default="{ row }">
            <span :style="{ color: row.side === 'buy' ? '#ef4444' : '#10b981' }">{{ row.side === 'buy' ? '买入' : '卖出' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="qty" label="数量" width="90" />
        <el-table-column prop="avg_fill_price" label="成交均价" width="100" />
        <el-table-column prop="status" label="状态" width="120"><template #default="{ row }"><order-status :status="row.status" /></template></el-table-column>
        <el-table-column prop="error_msg" label="说明" show-overflow-tooltip />
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import client from '../api/client'
import SignalStatus from '../components/SignalStatus.vue'
import OrderStatus from '../components/OrderStatus.vue'
import { fmt, ts } from '../utils'

const summary = ref({ brokers: {}, recent_signals: [], recent_orders: [] })
const tradingEnabled = ref(true)
const loading = ref(true)
let timer

const watchlist = ref([])

async function load() {
  summary.value = await client.get('/api/dashboard/summary')
  tradingEnabled.value = summary.value.trading_enabled
  loading.value = false
  watchlist.value = await client.get('/api/watchlist')
}

async function removeWatch(symbol) {
  await client.delete(`/api/watchlist/${symbol}`)
  watchlist.value = watchlist.value.filter((w) => w.symbol !== symbol)
}

async function toggleKillSwitch(value) {
  if (!value) {
    try {
      await ElMessageBox.confirm('确定要紧急停止全部交易吗？新信号将被风控直接拒绝。', 'Kill Switch', { type: 'warning' })
    } catch {
      tradingEnabled.value = true
      return
    }
  }
  await client.post('/api/risk/kill-switch', { enabled: value })
}

onMounted(() => {
  load()
  timer = setInterval(load, 10000)
})
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.stat-label { color: #6b7280; font-size: 13px; }
.stat-num { font-size: 30px; font-weight: 700; margin-top: 4px; }
.broker-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid #f3f4f6; }
</style>
