<template>
  <el-card>
    <div style="margin-bottom: 12px; display: flex; justify-content: space-between">
      <el-button type="primary" @click="openForm()">新建策略</el-button>
      <el-alert type="info" :closable="false" show-icon style="width: 640px"
                title="TradingView 告警里的 strategy 字段需与此处策略名一致；signal_only=仅提醒，live=真实下单" />
    </div>
    <el-table :data="items" v-loading="loading">
      <el-table-column prop="name" label="策略名" width="140" />
      <el-table-column prop="class_name" label="本地策略类" width="130"><template #default="{ row }">{{ row.class_name || '—(TV信号)' }}</template></el-table-column>
      <el-table-column prop="mode" label="模式" width="110">
        <template #default="{ row }">
          <el-tag :type="row.mode === 'live' ? 'danger' : 'info'" size="small">
            {{ row.mode === 'live' ? '实盘下单' : '仅提醒' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="broker" label="券商" width="90" />
      <el-table-column prop="default_qty" label="默认数量" width="100" />
      <el-table-column label="参数"><template #default="{ row }">{{ JSON.stringify(row.params) }}</template></el-table-column>
      <el-table-column prop="enabled" label="启用" width="90">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" @change="toggle(row)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="200">
        <template #default="{ row }">
          <el-button v-if="row.class_name" link type="success" :loading="runningId === row.id"
                     @click="runNow(row)">运行</el-button>
          <el-button link type="primary" @click="openForm(row)">编辑</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialog" :title="form.id ? '编辑策略' : '新建策略'" width="560px">
      <el-form :model="form" label-width="110px">
        <el-form-item label="策略名" required>
          <el-input v-model="form.name" placeholder="与 TV 告警 strategy 字段一致" />
        </el-form-item>
        <el-form-item label="本地策略类">
          <el-select v-model="form.class_name" clearable placeholder="留空 = 仅接收 TV 信号">
            <el-option v-for="b in builtin" :key="b.class_name"
                       :label="`${b.class_name}【${{ single: '单标的', portfolio: '组合', option: '期权' }[b.kind] || b.kind}】${b.doc}`"
                       :value="b.class_name" />
          </el-select>
        </el-form-item>
        <el-form-item label="模式">
          <el-radio-group v-model="form.mode">
            <el-radio value="signal_only">仅提醒（不下单）</el-radio>
            <el-radio value="live">实盘下单</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="执行账户">
          <el-select v-model="form.broker">
            <el-option v-for="a in accounts" :key="a.name" :label="`${a.name}（${a.type}）`" :value="a.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="默认数量">
          <el-input-number v-model="form.default_qty" :min="0" />
          <span style="color: #9ca3af; font-size: 12px; margin-left: 8px">TV 告警未带 qty 时使用</span>
        </el-form-item>
        <el-form-item label="参数(JSON)">
          <el-input v-model="paramsText" type="textarea" :rows="3" />
        </el-form-item>
        <template v-if="form.class_name">
          <el-form-item label="监控标的">
            <el-select v-model="form.symbols" multiple filterable allow-create default-first-option
                       placeholder="本地策略驱动的标的，如 US.AAPL / SH.600519" style="width: 100%" />
            <span v-if="currentKind === 'option'" style="color: #e6a23c; font-size: 12px">
              期权策略填正股符号（如 US.AAPL），合约由策略按虚值/到期规则自动选择；需先在风控设置启用期权交易
            </span>
          </el-form-item>
          <el-form-item label="K线周期">
            <el-select v-model="form.timeframe" style="width: 160px">
              <el-option label="日线" value="1d" />
              <el-option label="60分钟" value="60m" />
              <el-option label="15分钟" value="15m" />
              <el-option label="5分钟" value="5m" />
            </el-select>
          </el-form-item>
          <el-form-item label="运行时间(cron)">
            <el-input v-model="form.schedule_cron" placeholder="如 10 16 * * 1-5（北京时间；分钟级策略可用 */15 10-15 * * 1-5）" />
          </el-form-item>
        </template>
        <el-form-item label="备注">
          <el-input v-model="form.notes" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import client from '../api/client'

const items = ref([])
const builtin = ref([])
const loading = ref(false)
const dialog = ref(false)
const form = ref({})
const paramsText = ref('{}')
const runningId = ref(null)
const currentKind = computed(() =>
  builtin.value.find((b) => b.class_name === form.value.class_name)?.kind)

async function runNow(row) {
  runningId.value = row.id
  try {
    const summary = await client.post(`/api/strategies/${row.id}/run-now`)
    const errs = summary.errors?.length ? `；异常: ${summary.errors.join(' / ')}` : ''
    ElMessage.success(`运行完成：${summary.symbols} 个标的，产生 ${summary.signals} 个信号${errs}`)
  } finally {
    runningId.value = null
  }
}

async function load() {
  loading.value = true
  try {
    items.value = await client.get('/api/strategies')
  } finally {
    loading.value = false
  }
}

function openForm(row) {
  form.value = row
    ? { ...row, symbols: row.symbols || [] }
    : { name: '', class_name: null, mode: 'signal_only', broker: 'paper', default_qty: 0, params: {}, enabled: true, notes: '', symbols: [], schedule_cron: '', timeframe: '1d' }
  paramsText.value = JSON.stringify(form.value.params || {}, null, 0)
  dialog.value = true
}

async function save() {
  let params
  try {
    params = JSON.parse(paramsText.value || '{}')
  } catch {
    ElMessage.error('参数不是合法 JSON')
    return
  }
  const body = { ...form.value, params, schedule_cron: form.value.schedule_cron || null }
  if (form.value.id) await client.put(`/api/strategies/${form.value.id}`, body)
  else await client.post('/api/strategies', body)
  dialog.value = false
  ElMessage.success('已保存')
  load()
}

async function toggle(row) {
  await client.post(`/api/strategies/${row.id}/toggle`)
  load()
}

async function remove(row) {
  await ElMessageBox.confirm(`确定删除策略 ${row.name}？`, '删除', { type: 'warning' })
  await client.delete(`/api/strategies/${row.id}`)
  load()
}

const accounts = ref([])

onMounted(async () => {
  load()
  builtin.value = await client.get('/api/strategies/builtin')
  accounts.value = await client.get('/api/broker-accounts')
})
</script>
