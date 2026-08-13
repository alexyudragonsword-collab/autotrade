<template>
  <el-tag :type="map[status]?.type || 'info'" size="small">{{ map[status]?.text || status }}</el-tag>
</template>

<script setup>
defineProps({ status: String })
const map = {
  pending_submit: { text: '待提交', type: 'info' },
  submitted: { text: '已提交', type: 'primary' },
  partially_filled: { text: '部分成交', type: 'warning' },
  filled: { text: '已成交', type: 'success' },
  cancelled: { text: '已撤销', type: 'info' },
  rejected: { text: '已拒绝', type: 'danger' },
  failed: { text: '失败', type: 'danger' },
}
</script>
