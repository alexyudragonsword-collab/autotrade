<template>
  <el-container style="height: 100vh">
    <el-aside width="210px" class="aside">
      <div class="logo">⚡ AutoTrade</div>
      <el-menu :default-active="$route.path" router background-color="#1f2937" text-color="#d1d5db"
               active-text-color="#60a5fa" style="border: none">
        <el-menu-item index="/dashboard">📊 仪表盘</el-menu-item>
        <el-menu-item index="/signals">📡 信号日志</el-menu-item>
        <el-menu-item index="/orders">📋 订单持仓</el-menu-item>
        <el-menu-item index="/strategies">🧠 策略管理</el-menu-item>
        <el-menu-item index="/editor">✏️ 策略编辑器</el-menu-item>
        <el-menu-item index="/screener">🔍 选股器</el-menu-item>
        <el-menu-item index="/backtest">📈 回测中心</el-menu-item>
        <el-menu-item index="/risk">🛡️ 风控设置</el-menu-item>
        <el-menu-item index="/notify">🔔 通知设置</el-menu-item>
        <el-menu-item index="/settings">⚙️ 系统设置</el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header">
        <span>{{ $route.meta.title }}</span>
        <el-button link type="danger" @click="logout">退出登录</el-button>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { useRouter } from 'vue-router'

const router = useRouter()
function logout() {
  localStorage.removeItem('token')
  router.push('/login')
}
</script>

<style scoped>
.aside {
  background: #1f2937;
}
.logo {
  color: #fff;
  font-size: 18px;
  font-weight: bold;
  padding: 18px 20px;
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #fff;
  border-bottom: 1px solid #e5e7eb;
  font-size: 16px;
  font-weight: 600;
}
</style>
