<template>
  <el-row :gutter="16">
    <el-col :span="10">
      <el-card header="风控参数">
        <el-form :model="cfg" label-width="160px" v-loading="loading">
          <el-form-item label="交易总开关">
            <el-switch v-model="cfg.trading_enabled" active-text="允许" inactive-text="停止" />
          </el-form-item>
          <el-form-item label="单笔订单金额上限">
            <el-input-number v-model="cfg.max_order_value" :min="0" :step="10000" style="width: 200px" />
          </el-form-item>
          <el-form-item label="单标的持仓上限">
            <el-input-number v-model="cfg.max_position_value_per_symbol" :min="0" :step="10000" style="width: 200px" />
          </el-form-item>
          <el-form-item label="总敞口上限">
            <el-input-number v-model="cfg.max_total_exposure" :min="0" :step="50000" style="width: 200px" />
          </el-form-item>
          <el-form-item label="当日订单数上限">
            <el-input-number v-model="cfg.max_orders_per_day" :min="1" style="width: 200px" />
          </el-form-item>
          <el-form-item label="当日最大亏损">
            <el-input-number v-model="cfg.max_daily_loss" :min="0" :step="5000" style="width: 200px" />
          </el-form-item>
          <el-form-item label="标的白名单">
            <el-select v-model="cfg.symbol_whitelist" multiple filterable allow-create default-first-option
                       placeholder="留空 = 不限制；如 US.AAPL" style="width: 100%">
            </el-select>
          </el-form-item>
          <el-form-item label="交易时段检查">
            <el-switch v-model="cfg.trading_hours_enabled" />
            <span style="color: #9ca3af; font-size: 12px; margin-left: 8px">按各市场交易所时间校验</span>
          </el-form-item>
          <el-divider>持仓守护（0 = 关闭；每分钟检查，触发即市价全平并通知）</el-divider>
          <el-form-item label="止损 %">
            <el-input-number v-model="cfg.stop_loss_pct" :min="0" :max="100" :precision="1" style="width: 200px" />
            <span class="hint">亏损超过成本价该比例时平仓</span>
          </el-form-item>
          <el-form-item label="止盈 %">
            <el-input-number v-model="cfg.take_profit_pct" :min="0" :max="1000" :precision="1" style="width: 200px" />
            <span class="hint">盈利超过该比例时落袋</span>
          </el-form-item>
          <el-form-item label="移动止损 %">
            <el-input-number v-model="cfg.trailing_stop_pct" :min="0" :max="100" :precision="1" style="width: 200px" />
            <span class="hint">距持仓期最高价回撤该比例时平仓</span>
          </el-form-item>
          <el-button type="primary" @click="save">保存</el-button>
        </el-form>
      </el-card>
    </el-col>
    <el-col :span="14">
      <el-card header="风控拦截日志">
        <el-table :data="events" size="small" v-loading="eventsLoading">
          <el-table-column prop="ts" label="时间" width="165"><template #default="{ row }">{{ ts(row.ts) }}</template></el-table-column>
          <el-table-column prop="rule_name" label="规则" width="200" />
          <el-table-column label="订单意图" width="220">
            <template #default="{ row }">
              {{ row.order_intent?.symbol }} {{ row.order_intent?.side }} × {{ row.order_intent?.qty }}
            </template>
          </el-table-column>
          <el-table-column prop="reason" label="原因" show-overflow-tooltip />
        </el-table>
        <el-pagination style="margin-top: 12px" layout="total, prev, pager, next" :total="total"
                       :page-size="20" :current-page="page" @current-change="loadEvents" />
      </el-card>
    </el-col>
  </el-row>
</template>

<style scoped>
.hint { color: #9ca3af; font-size: 12px; margin-left: 8px; }
</style>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import client from '../api/client'
import { ts } from '../utils'

const cfg = ref({})
const loading = ref(true)
const events = ref([])
const total = ref(0)
const page = ref(1)
const eventsLoading = ref(false)

async function load() {
  cfg.value = await client.get('/api/risk')
  loading.value = false
}

async function save() {
  const { id, updated_at, ...body } = cfg.value
  await client.put('/api/risk', body)
  ElMessage.success('风控配置已保存')
}

async function loadEvents(p = 1) {
  page.value = p
  eventsLoading.value = true
  try {
    const data = await client.get('/api/risk/events', { params: { page: p, size: 20 } })
    events.value = data.items
    total.value = data.total
  } finally {
    eventsLoading.value = false
  }
}

onMounted(() => {
  load()
  loadEvents()
})
</script>
