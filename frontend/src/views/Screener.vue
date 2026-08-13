<template>
  <el-row :gutter="16">
    <el-col :span="9">
      <el-card header="选股器列表">
        <el-button type="primary" size="small" style="margin-bottom: 10px" @click="openForm()">新建选股器</el-button>
        <el-table :data="items" size="small" highlight-current-row @current-change="select">
          <el-table-column prop="name" label="名称" />
          <el-table-column prop="market" label="市场" width="70" />
          <el-table-column prop="schedule_cron" label="定时" width="110" />
          <el-table-column label="操作" width="150">
            <template #default="{ row }">
              <el-button link type="primary" size="small" :loading="runningId === row.id" @click.stop="run(row)">运行</el-button>
              <el-button link type="primary" size="small" @click.stop="openForm(row)">编辑</el-button>
              <el-button link type="danger" size="small" @click.stop="remove(row)">删</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </el-col>

    <el-col :span="15">
      <el-card :header="current ? `运行结果：${current.name}` : '运行结果'">
        <template v-if="results.length">
          <div v-for="r in results" :key="r.id" style="margin-bottom: 16px">
            <div style="display: flex; justify-content: space-between; margin-bottom: 6px">
              <span style="color: #6b7280; font-size: 13px">
                {{ ts(r.run_at) }} — 选出 <b>{{ r.count }}</b> 只
                <el-tag v-if="r.error_msg" type="danger" size="small">{{ r.error_msg }}</el-tag>
              </span>
              <el-button link type="primary" size="small" @click="notify(r)">推送到通知渠道</el-button>
            </div>
            <el-table :data="r.symbols" size="small" max-height="300">
              <el-table-column v-for="col in cols(r)" :key="col" :prop="col" :label="colName(col)" />
            </el-table>
          </div>
        </template>
        <el-empty v-else description="选择左侧选股器并点击运行" />
      </el-card>
    </el-col>
  </el-row>

  <el-dialog v-model="dialog" :title="form.id ? '编辑选股器' : '新建选股器'" width="680px">
    <el-form :model="form" label-width="100px">
      <el-form-item label="名称" required><el-input v-model="form.name" /></el-form-item>
      <el-form-item label="市场">
        <el-select v-model="form.market" style="width: 140px">
          <el-option label="A股 (CN)" value="CN" />
          <el-option label="美股 (US)" value="US" />
          <el-option label="港股 (HK)" value="HK" />
        </el-select>
      </el-form-item>
      <el-form-item label="标的池">
        <el-select v-model="form.universe.type" style="width: 140px">
          <el-option label="指数成分" value="index" />
          <el-option label="自定义列表" value="symbols" />
        </el-select>
        <el-input v-if="form.universe.type === 'index'" v-model="form.universe.value"
                  placeholder="沪深300 / 中证500 / 上证50 / US_MEGA" style="width: 300px; margin-left: 8px" />
        <el-input v-else v-model="symbolsText" placeholder="逗号分隔: 600519,000001 或 AAPL,MSFT"
                  style="width: 300px; margin-left: 8px" />
      </el-form-item>
      <el-divider>筛选条件（同时满足）</el-divider>
      <div v-for="(cond, i) in conditions" :key="i" style="display: flex; gap: 8px; margin-bottom: 8px">
        <el-select v-model="cond.kind" style="width: 110px">
          <el-option label="基本面" value="field" />
          <el-option label="表达式" value="expr" />
        </el-select>
        <template v-if="cond.kind === 'field'">
          <el-select v-model="cond.field" style="width: 150px">
            <el-option label="市盈率 pe_ttm" value="pe_ttm" />
            <el-option label="市净率 pb" value="pb" />
            <el-option label="总市值 market_cap" value="market_cap" />
            <el-option label="换手率 turnover_rate" value="turnover_rate" />
          </el-select>
          <el-select v-model="cond.op" style="width: 80px">
            <el-option v-for="op in ['<', '<=', '>', '>=']" :key="op" :label="op" :value="op" />
          </el-select>
          <el-input-number v-model="cond.value" :controls="false" style="width: 140px" />
        </template>
        <el-input v-else v-model="cond.expr" placeholder="如: close > SMA(close, 20) and RSI(close, 14) < 30" />
        <el-button link type="danger" @click="conditions.splice(i, 1)">删除</el-button>
      </div>
      <el-button size="small" @click="conditions.push({ kind: 'expr', expr: '' })">+ 添加条件</el-button>
      <el-form-item label="最多结果" style="margin-top: 12px">
        <el-input-number v-model="form.rules.limit" :min="1" :max="500" />
      </el-form-item>
      <el-form-item label="定时(cron)">
        <el-input v-model="form.schedule_cron" placeholder="如 30 15 * * 1-5（北京时间，留空不定时）" style="width: 300px" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialog = false">取消</el-button>
      <el-button type="primary" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import client from '../api/client'
import { ts } from '../utils'

const items = ref([])
const results = ref([])
const current = ref(null)
const runningId = ref(null)
const dialog = ref(false)
const form = ref({ universe: {}, rules: {} })
const conditions = ref([])
const symbolsText = ref('')

const COL_NAMES = {
  symbol: '代码', name: '名称', close: '收盘价', pe_ttm: 'PE', pb: 'PB',
  market_cap: '市值', last_price: '现价', turnover_rate: '换手率',
}
const colName = (c) => COL_NAMES[c] || c
function cols(r) {
  const first = r.symbols?.[0] || {}
  return Object.keys(first)
}

async function load() {
  items.value = await client.get('/api/screeners')
}

async function select(row) {
  if (!row) return
  current.value = row
  const data = await client.get(`/api/screeners/${row.id}/results`, { params: { size: 3 } })
  results.value = data.items
}

async function run(row) {
  runningId.value = row.id
  try {
    await client.post(`/api/screeners/${row.id}/run`)
    ElMessage.success('运行完成')
    current.value = row
    await select(row)
  } finally {
    runningId.value = null
  }
}

function openForm(row) {
  if (row) {
    form.value = JSON.parse(JSON.stringify(row))
    if (!form.value.universe.type) form.value.universe = { type: 'index', value: '' }
    if (form.value.universe.type === 'symbols') symbolsText.value = (form.value.universe.value || []).join(',')
    conditions.value = (form.value.rules.conditions || []).map((c) =>
      c.field ? { kind: 'field', ...c } : { kind: 'expr', expr: c.expr })
  } else {
    form.value = { name: '', market: 'CN', universe: { type: 'index', value: '沪深300' }, rules: { limit: 50 }, enabled: true, schedule_cron: '' }
    conditions.value = [{ kind: 'expr', expr: 'RSI(close, 14) < 30' }]
    symbolsText.value = ''
  }
  dialog.value = true
}

async function save() {
  const body = JSON.parse(JSON.stringify(form.value))
  if (body.universe.type === 'symbols') {
    body.universe.value = symbolsText.value.split(/[,，\s]+/).filter(Boolean)
  }
  body.rules.conditions = conditions.value
    .map((c) => (c.kind === 'field'
      ? { field: c.field, op: c.op, value: c.value }
      : { expr: c.expr }))
    .filter((c) => c.expr !== '' || c.field)
  body.schedule_cron = body.schedule_cron || null
  if (body.id) await client.put(`/api/screeners/${body.id}`, body)
  else await client.post('/api/screeners', body)
  dialog.value = false
  ElMessage.success('已保存')
  load()
}

async function remove(row) {
  await ElMessageBox.confirm(`确定删除选股器 ${row.name}？`, '删除', { type: 'warning' })
  await client.delete(`/api/screeners/${row.id}`)
  load()
}

async function notify(r) {
  await client.post(`/api/screeners/${current.value.id}/results/${r.id}/notify`)
  ElMessage.success('已推送')
}

onMounted(load)
</script>
