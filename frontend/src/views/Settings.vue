<template>
  <el-row :gutter="16">
    <el-col :span="12">
      <el-card header="TradingView Webhook">
        <el-form label-width="120px" v-loading="loading">
          <el-form-item label="Webhook 地址">
            <el-input :model-value="webhookUrl" readonly>
              <template #append>
                <el-button @click="copy(webhookUrl)">复制</el-button>
              </template>
            </el-input>
          </el-form-item>
          <el-form-item>
            <el-button type="warning" @click="rotate">重置 Token</el-button>
            <span style="color: #9ca3af; font-size: 12px; margin-left: 8px">重置后需更新 TradingView 里的告警地址</span>
          </el-form-item>
        </el-form>
        <el-divider>告警消息模板（粘贴到 TV 告警的"消息"框）</el-divider>
        <pre class="code">{{ alertTemplate }}</pre>
        <el-button size="small" @click="copy(alertTemplate)">复制模板</el-button>
      </el-card>
    </el-col>
    <el-col :span="12">
      <el-card>
        <template #header>
          <div style="display: flex; justify-content: space-between; align-items: center">
            <span>券商账户（可多实例）</span>
            <el-button type="primary" size="small" @click="openAccountForm">添加账户</el-button>
          </div>
        </template>
        <el-table :data="accounts" size="small">
          <el-table-column prop="name" label="账户名" width="110" />
          <el-table-column prop="type" label="类型" width="80" />
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <el-tag :type="row.connected ? 'success' : 'danger'" size="small">{{ row.connected ? '在线' : '离线' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="参数"><template #default="{ row }">{{ JSON.stringify(row.params) }}</template></el-table-column>
          <el-table-column prop="error" label="错误" show-overflow-tooltip />
          <el-table-column label="操作" width="180">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="reconnect(row.name)">重连</el-button>
              <el-button link :type="row.enabled ? 'warning' : 'success'" size="small"
                         @click="toggleAccount(row)">{{ row.enabled ? '停用' : '启用' }}</el-button>
              <el-button link type="danger" size="small" @click="removeAccount(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-alert type="info" :closable="false" show-icon style="margin-top: 12px"
                  title="密钥（富途解锁密码等）在服务器 .env 中配置。富途需 OpenD 网关，盈透需 TWS/IB Gateway；同一 Gateway 的多个 IBKR 账户需不同 client_id。" />
      </el-card>

      <el-dialog v-model="accountDialog" title="添加券商账户" width="480px">
        <el-form :model="accountForm" label-width="110px">
          <el-form-item label="类型">
            <el-select v-model="accountForm.type">
              <el-option label="paper（模拟）" value="paper" />
              <el-option label="futu（富途）" value="futu" />
              <el-option label="ibkr（盈透）" value="ibkr" />
            </el-select>
          </el-form-item>
          <el-form-item label="账户名">
            <el-input v-model="accountForm.name" placeholder="如 paper2 / futu_real / ibkr_paper" />
          </el-form-item>
          <template v-if="accountForm.type === 'paper'">
            <el-form-item label="初始资金">
              <el-input-number v-model="accountForm.params.initial_cash" :min="1000" :step="100000" style="width: 200px" />
            </el-form-item>
          </template>
          <template v-if="accountForm.type === 'futu'">
            <el-form-item label="OpenD 地址"><el-input v-model="accountForm.params.host" placeholder="127.0.0.1" /></el-form-item>
            <el-form-item label="端口"><el-input-number v-model="accountForm.params.port" :min="1" :max="65535" /></el-form-item>
            <el-form-item label="环境">
              <el-radio-group v-model="accountForm.params.trd_env">
                <el-radio value="SIMULATE">模拟</el-radio>
                <el-radio value="REAL">实盘</el-radio>
              </el-radio-group>
            </el-form-item>
          </template>
          <template v-if="accountForm.type === 'ibkr'">
            <el-form-item label="Gateway 地址"><el-input v-model="accountForm.params.host" placeholder="127.0.0.1" /></el-form-item>
            <el-form-item label="端口"><el-input-number v-model="accountForm.params.port" :min="1" :max="65535" /></el-form-item>
            <el-form-item label="client_id"><el-input-number v-model="accountForm.params.client_id" :min="1" :max="999" /></el-form-item>
          </template>
        </el-form>
        <template #footer>
          <el-button @click="accountDialog = false">取消</el-button>
          <el-button type="primary" @click="saveAccount">保存并连接</el-button>
        </template>
      </el-dialog>
    </el-col>
  </el-row>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import client from '../api/client'

const settings = ref({})
const brokers = ref({})
const accounts = ref([])
const loading = ref(true)
const accountDialog = ref(false)
const accountForm = ref({ type: 'paper', name: '', params: {} })

function openAccountForm() {
  accountForm.value = { type: 'paper', name: '', params: { initial_cash: 1000000 } }
  accountDialog.value = true
}

async function saveAccount() {
  await client.post('/api/broker-accounts', accountForm.value)
  accountDialog.value = false
  ElMessage.success('账户已添加')
  load()
}

async function toggleAccount(row) {
  await client.post(`/api/broker-accounts/${row.id}/toggle`)
  load()
}

async function removeAccount(row) {
  await ElMessageBox.confirm(`确定删除账户 ${row.name}？`, '删除', { type: 'warning' })
  await client.delete(`/api/broker-accounts/${row.id}`)
  ElMessage.success('已删除')
  load()
}

const webhookUrl = computed(() =>
  settings.value.webhook_path ? `${location.origin}${settings.value.webhook_path}` : '')

const alertTemplate = computed(() => JSON.stringify({
  secret: '<你的 WEBHOOK_SECRET>',
  alert_id: '{{timenow}}-{{ticker}}-buy',
  strategy: '策略配置中的策略名',
  symbol: '{{ticker}}',
  exchange: '{{exchange}}',
  action: 'buy',
  qty: 100,
  order_type: 'market',
  price: '{{close}}',
}, null, 2))

const brokerRows = computed(() =>
  Object.entries(brokers.value).map(([name, info]) => ({ name, ...info })))

async function load() {
  settings.value = await client.get('/api/settings')
  brokers.value = await client.get('/api/brokers/status')
  accounts.value = await client.get('/api/broker-accounts')
  loading.value = false
}

async function rotate() {
  await ElMessageBox.confirm('重置后旧地址立即失效，TradingView 告警需要更新。确定？', '重置 Token', { type: 'warning' })
  await client.post('/api/settings/webhook-token/rotate')
  ElMessage.success('已重置')
  load()
}

async function reconnect(name) {
  try {
    await client.post(`/api/brokers/${name}/reconnect`)
    ElMessage.success('重连成功')
  } finally {
    load()
  }
}

function copy(text) {
  navigator.clipboard.writeText(text)
  ElMessage.success('已复制')
}

onMounted(load)
</script>

<style scoped>
.code {
  background: #1f2937;
  color: #d1d5db;
  padding: 12px;
  border-radius: 6px;
  font-size: 12px;
  overflow: auto;
}
</style>
