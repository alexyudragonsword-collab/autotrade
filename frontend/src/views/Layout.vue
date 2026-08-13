<template>
  <el-container style="height: 100vh">
    <el-aside width="210px" class="aside">
      <div class="logo">⚡ AutoTrade</div>
      <el-menu :default-active="$route.path" router background-color="#1f2937" text-color="#d1d5db"
               active-text-color="#60a5fa" style="border: none">
        <el-menu-item index="/dashboard">📊 {{ $t('仪表盘') }}</el-menu-item>
        <el-menu-item index="/signals">📡 {{ $t('信号日志') }}</el-menu-item>
        <el-menu-item index="/orders">📋 {{ $t('订单持仓') }}</el-menu-item>
        <el-menu-item index="/options">🎯 {{ $t('期权链') }}</el-menu-item>
        <el-menu-item index="/strategies">🧠 {{ $t('策略管理') }}</el-menu-item>
        <el-menu-item index="/editor">✏️ {{ $t('策略编辑器') }}</el-menu-item>
        <el-menu-item index="/screener">🔍 {{ $t('选股器') }}</el-menu-item>
        <el-menu-item index="/backtest">📈 {{ $t('回测中心') }}</el-menu-item>
        <el-menu-item index="/performance">💰 {{ $t('绩效分析') }}</el-menu-item>
        <el-menu-item index="/risk">🛡️ {{ $t('风控设置') }}</el-menu-item>
        <el-menu-item index="/notify">🔔 {{ $t('通知设置') }}</el-menu-item>
        <el-menu-item index="/settings">⚙️ {{ $t('系统设置') }}</el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header">
        <span>{{ $t($route.meta.title || '') }}</span>
        <span>
          <el-radio-group :model-value="locale" size="small" style="margin-right: 16px"
                          @change="switchLang">
            <el-radio-button value="zh">中文</el-radio-button>
            <el-radio-button value="en">EN</el-radio-button>
          </el-radio-group>
          <el-button link type="danger" @click="logout">{{ $t('退出登录') }}</el-button>
        </span>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { connectWs, disconnectWs } from '../ws'
import { setLang } from '../i18n'

const router = useRouter()
const { locale } = useI18n()

function switchLang(lang) {
  setLang(lang)
}

function logout() {
  localStorage.removeItem('token')
  disconnectWs()
  router.push('/login')
}

onMounted(connectWs)
onUnmounted(disconnectWs)
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
