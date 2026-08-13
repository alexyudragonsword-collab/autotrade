<template>
  <el-row :gutter="16">
    <el-col :span="6">
      <el-card header="自定义策略">
        <el-button type="primary" size="small" style="margin-bottom: 10px" @click="newStrategy">新建策略</el-button>
        <el-table :data="items" size="small" highlight-current-row @current-change="select">
          <el-table-column prop="class_name" label="类名" />
          <el-table-column prop="enabled" label="启用" width="60">
            <template #default="{ row }">{{ row.enabled ? '✅' : '⏸️' }}</template>
          </el-table-column>
        </el-table>
        <el-alert type="warning" :closable="false" show-icon style="margin-top: 12px"
                  title="策略代码以完整 Python 权限在服务器执行，请勿粘贴来源不明的代码。" />
      </el-card>
    </el-col>
    <el-col :span="18">
      <el-card>
        <template #header>
          <div style="display: flex; justify-content: space-between; align-items: center">
            <span>{{ current?.id ? `编辑：${current.class_name}` : '新建策略' }}</span>
            <span>
              <el-button size="small" @click="loadTemplate">插入模板</el-button>
              <el-button size="small" type="warning" :loading="validating" @click="validate">校验试跑</el-button>
              <el-button size="small" type="primary" :loading="saving" @click="save">保存</el-button>
              <el-button v-if="current?.id" size="small" type="danger" @click="remove">删除</el-button>
            </span>
          </div>
        </template>
        <el-form label-width="80px">
          <el-form-item label="类名">
            <el-input v-model="className" placeholder="与代码中定义的类名一致，如 MyStrategy" style="width: 300px" />
            <el-switch v-model="enabled" active-text="启用" style="margin-left: 16px" />
          </el-form-item>
        </el-form>
        <textarea v-model="code" class="editor" spellcheck="false"
                  placeholder="# 在这里编写策略代码（可用: Strategy/PortfolioStrategy 基类、pd/np、SMA/EMA/RSI/MACD/ATR 等指标函数）" />
        <el-alert v-if="report" :type="report.ok ? 'success' : 'error'" :closable="false" show-icon
                  style="margin-top: 10px"
                  :title="report.ok
                    ? `校验通过：${report.detected_class}（${report.kind === 'portfolio' ? '组合策略' : '单标的策略'}），试跑产生 ${report.trades} 笔交易，参数 ${JSON.stringify(report.params)}`
                    : report.error" />
        <div style="color: #9ca3af; font-size: 12px; margin-top: 8px">
          保存后即可在「策略管理」绑定实盘、在「回测中心」直接回测（类名出现在策略下拉中）。
          单标的策略实现 <code>on_bar(self, ctx)</code>；组合策略继承 PortfolioStrategy 实现
          <code>on_rebalance(self, ctx)</code>。
        </div>
      </el-card>
    </el-col>
  </el-row>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import client from '../api/client'

const items = ref([])
const current = ref(null)
const className = ref('')
const code = ref('')
const enabled = ref(true)
const report = ref(null)
const validating = ref(false)
const saving = ref(false)

async function load() {
  items.value = await client.get('/api/custom-strategies')
}

function select(row) {
  if (!row) return
  current.value = row
  className.value = row.class_name
  code.value = row.code
  enabled.value = row.enabled
  report.value = null
}

function newStrategy() {
  current.value = null
  className.value = ''
  code.value = ''
  enabled.value = true
  report.value = null
}

async function loadTemplate() {
  const data = await client.get('/api/custom-strategies/template')
  code.value = data.template
  if (!className.value) className.value = 'MyStrategy'
}

async function validate() {
  validating.value = true
  report.value = null
  try {
    report.value = await client.post('/api/custom-strategies/validate',
      { class_name: className.value || 'X', code: code.value })
  } catch (e) {
    report.value = { ok: false, error: e.response?.data?.detail || String(e) }
  } finally {
    validating.value = false
  }
}

async function save() {
  saving.value = true
  try {
    const body = { class_name: className.value, code: code.value, enabled: enabled.value }
    if (current.value?.id) {
      current.value = await client.put(`/api/custom-strategies/${current.value.id}`, body)
    } else {
      current.value = await client.post('/api/custom-strategies', body)
    }
    ElMessage.success('已保存并生效（热加载，无需重启）')
    load()
  } catch (e) {
    report.value = { ok: false, error: e.response?.data?.detail || String(e) }
  } finally {
    saving.value = false
  }
}

async function remove() {
  await ElMessageBox.confirm(`确定删除策略 ${current.value.class_name}？`, '删除', { type: 'warning' })
  await client.delete(`/api/custom-strategies/${current.value.id}`)
  newStrategy()
  load()
}

onMounted(load)
</script>

<style scoped>
.editor {
  width: 100%;
  height: 460px;
  font-family: 'JetBrains Mono', Consolas, Menlo, monospace;
  font-size: 13px;
  line-height: 1.6;
  padding: 12px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  background: #1f2937;
  color: #e5e7eb;
  resize: vertical;
  box-sizing: border-box;
  tab-size: 4;
}
.editor:focus { outline: 2px solid #60a5fa; }
</style>
