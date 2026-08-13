<template>
  <el-tabs v-model="tab">
    <el-tab-pane :label="$t('订单')" name="orders">
      <el-card>
        <div style="margin-bottom: 12px; display: flex; justify-content: space-between">
          <span>
            <el-select v-model="status" :placeholder="$t('全部状态')" clearable style="width: 150px" @change="loadOrders(1)">
              <el-option v-for="s in ['submitted', 'partially_filled', 'filled', 'cancelled', 'rejected', 'failed']"
                         :key="s" :label="s" :value="s" />
            </el-select>
            <el-button style="margin-left: 8px" @click="loadOrders(page)">{{ $t('刷新') }}</el-button>
          </span>
          <el-button type="primary" @click="manualDialog = true">{{ $t('手动下单') }}</el-button>
        </div>
        <el-table :data="orders" v-loading="loading">
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column prop="created_at" :label="$t('时间')" width="165"><template #default="{ row }">{{ ts(row.created_at) }}</template></el-table-column>
          <el-table-column prop="broker" :label="$t('券商')" width="90" />
          <el-table-column prop="symbol" :label="$t('标的')" width="110" />
          <el-table-column :label="$t('方向')" width="70">
            <template #default="{ row }">
              <span :style="{ color: row.side === 'buy' ? '#ef4444' : '#10b981' }">{{ $t(row.side === 'buy' ? '买入' : '卖出') }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="order_type" :label="$t('类型')" width="80" />
          <el-table-column prop="qty" :label="$t('数量')" width="90" />
          <el-table-column prop="limit_price" :label="$t('限价')" width="90" />
          <el-table-column prop="filled_qty" :label="$t('已成交')" width="90" />
          <el-table-column prop="avg_fill_price" :label="$t('均价')" width="90" />
          <el-table-column prop="status" :label="$t('状态')" width="110">
            <template #default="{ row }"><order-status :status="row.status" /></template>
          </el-table-column>
          <el-table-column prop="error_msg" :label="$t('说明')" show-overflow-tooltip />
          <el-table-column :label="$t('操作')" width="90">
            <template #default="{ row }">
              <el-button v-if="['submitted', 'partially_filled'].includes(row.status)" link type="danger"
                         @click="cancel(row.id)">{{ $t('撤单') }}</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination style="margin-top: 12px" layout="total, prev, pager, next" :total="total"
                       :page-size="20" :current-page="page" @current-change="loadOrders" />
      </el-card>
    </el-tab-pane>

    <el-tab-pane :label="$t('持仓')" name="positions">
      <el-card>
        <div style="margin-bottom: 12px">
          <el-button type="primary" @click="syncPositions" :loading="syncing">{{ $t('同步券商持仓') }}</el-button>
        </div>
        <el-table :data="positions" v-loading="posLoading">
          <el-table-column prop="broker" :label="$t('券商')" width="100" />
          <el-table-column prop="symbol" :label="$t('标的')" width="130" />
          <el-table-column prop="market" :label="$t('市场')" width="80" />
          <el-table-column prop="qty" :label="$t('数量')" width="110">
            <template #default="{ row }">
              <span :style="{ color: row.qty < 0 ? '#10b981' : '' }">{{ row.qty }}</span>
              <el-tag v-if="row.qty < 0" type="success" size="small" style="margin-left: 4px">{{ $t('空头') }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="multiplier" :label="$t('乘数')" width="70">
            <template #default="{ row }">{{ row.multiplier > 1 ? `×${row.multiplier}` : '-' }}</template>
          </el-table-column>
          <el-table-column prop="avg_cost" :label="$t('成本价')" width="110"><template #default="{ row }">{{ fmt(row.avg_cost) }}</template></el-table-column>
          <el-table-column prop="last_price" :label="$t('现价')" width="110"><template #default="{ row }">{{ fmt(row.last_price) }}</template></el-table-column>
          <el-table-column prop="unrealized_pnl" :label="$t('浮动盈亏')" width="130">
            <template #default="{ row }">
              <span :style="{ color: (row.unrealized_pnl || 0) >= 0 ? '#ef4444' : '#10b981' }">{{ fmt(row.unrealized_pnl) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="last_sync_at" :label="$t('同步时间')"><template #default="{ row }">{{ ts(row.last_sync_at) }}</template></el-table-column>
        </el-table>
      </el-card>
    </el-tab-pane>
  </el-tabs>

  <manual-order-dialog v-model="manualDialog" @placed="loadOrders(1)" />
</template>

<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import client from '../api/client'
import { tr } from '../i18n'
import OrderStatus from '../components/OrderStatus.vue'
import ManualOrderDialog from '../components/ManualOrderDialog.vue'
import { fmt, ts } from '../utils'
import { onEvent } from '../ws'

const tab = ref('orders')
const orders = ref([])
const total = ref(0)
const page = ref(1)
const status = ref('')
const loading = ref(false)
const positions = ref([])
const posLoading = ref(false)
const syncing = ref(false)
const manualDialog = ref(false)

async function loadOrders(p = 1) {
  page.value = p
  loading.value = true
  try {
    const data = await client.get('/api/orders', { params: { page: p, size: 20, status: status.value || undefined } })
    orders.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

async function cancel(id) {
  await ElMessageBox.confirm(tr('确定撤销该订单？'), tr('撤单'), { type: 'warning' })
  await client.post(`/api/orders/${id}/cancel`)
  ElMessage.success(tr('撤单请求已发送'))
  loadOrders(page.value)
}

async function loadPositions() {
  posLoading.value = true
  try {
    positions.value = await client.get('/api/positions')
  } finally {
    posLoading.value = false
  }
}

async function syncPositions() {
  syncing.value = true
  try {
    await client.post('/api/positions/sync')
    await loadPositions()
    ElMessage.success(tr('持仓已同步'))
  } finally {
    syncing.value = false
  }
}

const accounts = ref([])
let offOrder

watch(tab, (v) => { if (v === 'positions') loadPositions() })
onMounted(async () => {
  loadOrders()
  offOrder = onEvent('order_update', () => {
    if (tab.value === 'orders') loadOrders(page.value)
    else loadPositions()
  })
  accounts.value = await client.get('/api/broker-accounts')
})
onUnmounted(() => offOrder?.())
</script>
