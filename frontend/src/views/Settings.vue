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
      <el-card header="券商连接">
        <el-table :data="brokerRows" size="small">
          <el-table-column prop="name" label="券商" width="110" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="row.connected ? 'success' : 'danger'" size="small">{{ row.connected ? '在线' : '离线' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="error" label="错误信息" show-overflow-tooltip />
          <el-table-column label="操作" width="90">
            <template #default="{ row }">
              <el-button link type="primary" @click="reconnect(row.name)">重连</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-alert type="info" :closable="false" show-icon style="margin-top: 12px"
                  title="券商启用与密钥在服务器 .env 中配置（FUTU_* / IBKR_*），修改后重启服务生效。富途需运行 OpenD 网关，盈透需运行 TWS 或 IB Gateway。" />
      </el-card>
    </el-col>
  </el-row>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import client from '../api/client'

const settings = ref({})
const brokers = ref({})
const loading = ref(true)

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
