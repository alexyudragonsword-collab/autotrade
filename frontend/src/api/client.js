import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '../router'

const client = axios.create({ baseURL: '/', timeout: 30000 })

client.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

client.interceptors.response.use(
  (resp) => resp.data,
  (error) => {
    const status = error.response?.status
    if (status === 401) {
      localStorage.removeItem('token')
      router.push('/login')
    } else {
      const detail = error.response?.data?.detail || error.message
      ElMessage.error(String(detail))
    }
    return Promise.reject(error)
  },
)

export default client
