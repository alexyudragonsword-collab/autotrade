<template>
  <el-card :header="$t('通知渠道')">
    <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px"
              :title="$t('渠道密钥（Bot Token / SMTP 密码 / Webhook 地址）在服务器 .env 中配置；此处控制启用状态与最低通知级别。')" />
    <el-button type="primary" size="small" style="margin-bottom: 12px" @click="openForm()">{{ $t('添加渠道') }}</el-button>
    <el-table :data="items" v-loading="loading">
      <el-table-column prop="type" :label="$t('类型')" width="130">
        <template #default="{ row }">{{ $t(typeNames[row.type] || row.type) }}</template>
      </el-table-column>
      <el-table-column prop="name" :label="$t('名称')" width="160" />
      <el-table-column prop="min_level" :label="$t('最低级别')" width="120">
        <template #default="{ row }">
          <el-tag :type="{ info: 'info', warn: 'warning', error: 'danger' }[row.min_level]" size="small">{{ row.min_level }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="enabled" :label="$t('启用')" width="90">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" @change="(v) => toggle(row, v)" />
        </template>
      </el-table-column>
      <el-table-column :label="$t('操作')" width="220">
        <template #default="{ row }">
          <el-button link type="primary" :loading="testingId === row.id" @click="test(row)">{{ $t('发送测试') }}</el-button>
          <el-button link type="primary" @click="openForm(row)">{{ $t('编辑') }}</el-button>
          <el-button link type="danger" @click="remove(row)">{{ $t('删除') }}</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialog" :title="form.id ? $t('编辑渠道') : $t('添加渠道')" width="440px">
      <el-form :model="form" label-width="100px">
        <el-form-item :label="$t('类型')">
          <el-select v-model="form.type" :disabled="!!form.id">
            <el-option label="Telegram" value="telegram" />
            <el-option :label="$t('邮件')" value="email" />
            <el-option :label="$t('企业微信')" value="wecom" />
            <el-option :label="$t('钉钉')" value="dingtalk" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('名称')"><el-input v-model="form.name" /></el-form-item>
        <el-form-item :label="$t('最低级别')">
          <el-select v-model="form.min_level">
            <el-option :label="$t('info（全部）')" value="info" />
            <el-option :label="$t('warn（警告及以上）')" value="warn" />
            <el-option :label="$t('error（仅错误）')" value="error" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('限定策略')">
          <el-select v-model="filterStrategies" multiple clearable :placeholder="$t('留空 = 全部策略')" style="width: 100%">
            <el-option v-for="s in strategyOptions" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('限定账户')">
          <el-select v-model="filterBrokers" multiple clearable :placeholder="$t('留空 = 全部账户')" style="width: 100%">
            <el-option v-for="a in accountOptions" :key="a" :label="a" :value="a" />
          </el-select>
        </el-form-item>
        <div style="color: #9ca3af; font-size: 12px; margin-left: 100px">
          {{ $t('系统级事件（kill switch 等）不受限定影响，始终投递到本渠道。') }}
        </div>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">{{ $t('取消') }}</el-button>
        <el-button type="primary" @click="save">{{ $t('保存') }}</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import client from '../api/client'
import { tr } from '../i18n'

const items = ref([])
const loading = ref(false)
const dialog = ref(false)
const form = ref({})
const testingId = ref(null)
const typeNames = { telegram: 'Telegram', email: '邮件', wecom: '企业微信', dingtalk: '钉钉' }
const filterStrategies = ref([])
const filterBrokers = ref([])
const strategyOptions = ref([])
const accountOptions = ref([])

async function loadOptions() {
  try {
    const strategies = await client.get('/api/strategies')
    strategyOptions.value = strategies.map((s) => s.name)
    const accounts = await client.get('/api/broker-accounts')
    accountOptions.value = accounts.map((a) => a.name)
  } catch { /* 选项加载失败不阻塞页面 */ }
}

async function load() {
  loading.value = true
  try {
    items.value = await client.get('/api/notify/channels')
  } finally {
    loading.value = false
  }
}

function openForm(row) {
  form.value = row ? { ...row } : { type: 'telegram', name: '', enabled: true, min_level: 'info' }
  filterStrategies.value = row?.config?.strategies || []
  filterBrokers.value = row?.config?.brokers || []
  dialog.value = true
}

function _body(row, overrides = {}) {
  return {
    type: row.type, name: row.name, enabled: row.enabled, min_level: row.min_level,
    config: row.config || {},
    ...overrides,
  }
}

async function save() {
  const body = _body(form.value, {
    config: { strategies: filterStrategies.value, brokers: filterBrokers.value },
  })
  if (form.value.id) await client.put(`/api/notify/channels/${form.value.id}`, body)
  else await client.post('/api/notify/channels', body)
  dialog.value = false
  load()
}

async function toggle(row, v) {
  await client.put(`/api/notify/channels/${row.id}`, _body(row, { enabled: v }))
  load()
}

async function test(row) {
  testingId.value = row.id
  try {
    await client.post(`/api/notify/channels/${row.id}/test`)
    ElMessage.success(tr('测试消息已发送，请检查是否收到'))
  } finally {
    testingId.value = null
  }
}

async function remove(row) {
  await ElMessageBox.confirm(tr('确定删除该渠道？'), tr('删除'), { type: 'warning' })
  await client.delete(`/api/notify/channels/${row.id}`)
  load()
}

onMounted(() => {
  load()
  loadOptions()
})
</script>
