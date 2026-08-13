<template>
  <el-card>
    <div style="margin-bottom: 12px">
      <el-select v-model="status" :placeholder="$t('全部状态')" clearable style="width: 160px" @change="load(1)">
        <el-option v-for="(v, k) in statusOptions" :key="k" :label="$t(v)" :value="k" />
      </el-select>
      <el-button style="margin-left: 8px" @click="load(page)">{{ $t('刷新') }}</el-button>
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
import client from '../api/client'
import SignalStatus from '../components/SignalStatus.vue'
import { ts } from '../utils'

const items = ref([])
const total = ref(0)
const page = ref(1)
const status = ref('')
const loading = ref(false)
const drawer = ref(false)
const detail = ref({})

const statusOptions = {
  received: '已接收', rejected_risk: '风控拦截', rejected: '已拒绝',
  routed: '已下单', executed: '已执行', failed: '失败',
}

async function load(p = 1) {
  page.value = p
  loading.value = true
  try {
    const data = await client.get('/api/signals', { params: { page: p, size: 20, status: status.value || undefined } })
    items.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

async function showDetail(id) {
  detail.value = await client.get(`/api/signals/${id}`)
  drawer.value = true
}

onMounted(() => load())
</script>
