<template>
  <el-tag :type="map[status]?.type || 'info'" size="small">{{ map[status]?.text || status }}</el-tag>
</template>

<script setup>
defineProps({ status: String })
const map = {
  received: { text: '已接收', type: 'info' },
  duplicate: { text: '重复', type: 'info' },
  rejected_risk: { text: '风控拦截', type: 'danger' },
  rejected: { text: '已拒绝', type: 'warning' },
  routed: { text: '已下单', type: 'primary' },
  executed: { text: '已执行', type: 'success' },
  failed: { text: '失败', type: 'danger' },
}
</script>
