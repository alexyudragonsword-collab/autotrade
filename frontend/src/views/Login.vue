<template>
  <div class="login-wrap">
    <el-card class="login-card">
      <h2 style="text-align: center">{{ $t('AutoTrade 自动交易系统') }}</h2>
      <el-form @submit.prevent="doLogin">
        <el-form-item>
          <el-input v-model="username" :placeholder="$t('用户名')" size="large" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="password" type="password" :placeholder="$t('密码')" size="large" show-password />
        </el-form-item>
        <el-button type="primary" size="large" style="width: 100%" :loading="loading" native-type="submit">
          {{ $t('登 录') }}
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import client from '../api/client'

const username = ref('')
const password = ref('')
const loading = ref(false)
const router = useRouter()

async function doLogin() {
  loading.value = true
  try {
    const data = await client.post('/api/auth/login', {
      username: username.value,
      password: password.value,
    })
    localStorage.setItem('token', data.access_token)
    router.push('/dashboard')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrap {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  background: linear-gradient(135deg, #1f2937 0%, #111827 100%);
}
.login-card {
  width: 380px;
  padding: 12px;
}
</style>
