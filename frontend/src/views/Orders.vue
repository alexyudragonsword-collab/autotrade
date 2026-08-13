<template>
  <el-tabs v-model="tab">
    <el-tab-pane label="订单" name="orders">
      <el-card>
        <div style="margin-bottom: 12px">
          <el-select v-model="status" placeholder="全部状态" clearable style="width: 150px" @change="loadOrders(1)">
            <el-option v-for="s in ['submitted', 'partially_filled', 'filled', 'cancelled', 'rejected', 'failed']"
                       :key="s" :label="s" :value="s" />
          </el-select>
          <el-button style="margin-left: 8px" @click="loadOrders(page)">刷新</el-button>
        </div>
        <el-table :data="orders" v-loading="loading">
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column prop="created_at" label="时间" width="165"><template #default="{ row }">{{ ts(row.created_at) }}</template></el-table-column>
          <el-table-column prop="broker" label="券商" width="90" />
          <el-table-column prop="symbol" label="标的" width="110" />
          <el-table-column label="方向" width="70">
            <template #default="{ row }">
              <span :style="{ color: row.side === 'buy' ? '#ef4444' : '#10b981' }">{{ row.side === 'buy' ? '买入' : '卖出' }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="order_type" label="类型" width="80" />
          <el-table-column prop="qty" label="数量" width="90" />
          <el-table-column prop="limit_price" label="限价" width="90" />
          <el-table-column prop="filled_qty" label="已成交" width="90" />
          <el-table-column prop="avg_fill_price" label="均价" width="90" />
          <el-table-column prop="status" label="状态" width="110">
            <template #default="{ row }"><order-status :status="row.status" /></template>
          </el-table-column>
          <el-table-column prop="error_msg" label="说明" show-overflow-tooltip />
          <el-table-column label="操作" width="90">
            <template #default="{ row }">
              <el-button v-if="['submitted', 'partially_filled'].includes(row.status)" link type="danger"
                         @click="cancel(row.id)">撤单</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination style="margin-top: 12px" layout="total, prev, pager, next" :total="total"
                       :page-size="20" :current-page="page" @current-change="loadOrders" />
      </el-card>
    </el-tab-pane>

    <el-tab-pane label="持仓" name="positions">
      <el-card>
        <div style="margin-bottom: 12px">
          <el-button type="primary" @click="syncPositions" :loading="syncing">同步券商持仓</el-button>
        </div>
        <el-table :data="positions" v-loading="posLoading">
          <el-table-column prop="broker" label="券商" width="100" />
          <el-table-column prop="symbol" label="标的" width="130" />
          <el-table-column prop="market" label="市场" width="80" />
          <el-table-column prop="qty" label="数量" width="110" />
          <el-table-column prop="avg_cost" label="成本价" width="110"><template #default="{ row }">{{ fmt(row.avg_cost) }}</template></el-table-column>
          <el-table-column prop="last_price" label="现价" width="110"><template #default="{ row }">{{ fmt(row.last_price) }}</template></el-table-column>
          <el-table-column prop="unrealized_pnl" label="浮动盈亏" width="130">
            <template #default="{ row }">
              <span :style="{ color: (row.unrealized_pnl || 0) >= 0 ? '#ef4444' : '#10b981' }">{{ fmt(row.unrealized_pnl) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="last_sync_at" label="同步时间"><template #default="{ row }">{{ ts(row.last_sync_at) }}</template></el-table-column>
        </el-table>
      </el-card>
    </el-tab-pane>
  </el-tabs>
</template>

<script setup>
import { onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import client from '../api/client'
import OrderStatus from '../components/OrderStatus.vue'
import { fmt, ts } from '../utils'

const tab = ref('orders')
const orders = ref([])
const total = ref(0)
const page = ref(1)
const status = ref('')
const loading = ref(false)
const positions = ref([])
const posLoading = ref(false)
const syncing = ref(false)

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
  await ElMessageBox.confirm('确定撤销该订单？', '撤单', { type: 'warning' })
  await client.post(`/api/orders/${id}/cancel`)
  ElMessage.success('撤单请求已发送')
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
    ElMessage.success('持仓已同步')
  } finally {
    syncing.value = false
  }
}

watch(tab, (v) => { if (v === 'positions') loadPositions() })
onMounted(() => loadOrders())
</script>
