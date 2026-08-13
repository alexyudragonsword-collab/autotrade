<template>
  <el-dialog :model-value="modelValue" title="手动下单（经过风控）" width="460px"
             @update:model-value="$emit('update:modelValue', $event)">
    <el-form :model="form" label-width="90px">
      <el-form-item label="账户">
        <el-select v-model="form.broker">
          <el-option v-for="a in accounts" :key="a.name" :label="`${a.name}（${a.type}）`" :value="a.name" />
        </el-select>
      </el-form-item>
      <el-form-item label="标的">
        <el-input v-model="form.symbol" placeholder="US.AAPL / HK.00700 / 期权完整符号" />
        <div v-if="optionInfo" style="font-size: 12px; color: #6b7280">
          期权：{{ optionInfo.underlying }} 到期 {{ optionInfo.expiry }}
          {{ optionInfo.right === 'C' ? 'Call' : 'Put' }} 行权价 {{ optionInfo.strike }}
        </div>
      </el-form-item>
      <el-form-item label="方向">
        <el-radio-group v-model="form.side">
          <el-radio value="buy">买入</el-radio>
          <el-radio value="sell">卖出</el-radio>
        </el-radio-group>
        <span v-if="optionInfo && form.side === 'sell'" style="color: #e6a23c; font-size: 12px; margin-left: 8px">
          卖出无持仓即为卖方开仓（收权利金）
        </span>
      </el-form-item>
      <el-form-item label="类型">
        <el-radio-group v-model="form.order_type">
          <el-radio value="market">市价</el-radio>
          <el-radio value="limit">限价</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="数量">
        <el-input-number v-model="form.qty" :min="1" style="width: 180px" />
        <span v-if="optionInfo" style="color: #9ca3af; font-size: 12px; margin-left: 8px">张（每张乘数见期权链）</span>
      </el-form-item>
      <el-form-item v-if="form.order_type === 'limit'" label="限价">
        <el-input-number v-model="form.limit_price" :min="0" :precision="3" style="width: 180px" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="$emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" :loading="placing" @click="place">下单</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import client from '../api/client'

const props = defineProps({
  modelValue: Boolean,
  prefill: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'placed'])

const accounts = ref([])
const placing = ref(false)
const form = ref({ broker: 'paper', symbol: '', side: 'buy', order_type: 'market', qty: 1, limit_price: null })

const optionInfo = computed(() => {
  const parts = (form.value.symbol || '').split('|')
  if (parts.length !== 4) return null
  return { underlying: parts[0], expiry: parts[1], right: parts[2], strike: parts[3] }
})

watch(() => props.modelValue, (open) => {
  if (open && props.prefill) form.value = { ...form.value, ...props.prefill }
})

async function place() {
  placing.value = true
  try {
    const data = await client.post('/api/manual-order', form.value)
    ElMessage.success(`订单 #${data.order_id} 已提交（${data.status}）`)
    emit('update:modelValue', false)
    emit('placed', data)
  } finally {
    placing.value = false
  }
}

onMounted(async () => {
  accounts.value = await client.get('/api/broker-accounts')
})
</script>
