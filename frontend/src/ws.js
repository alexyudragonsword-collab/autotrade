// WebSocket 实时推送客户端：自动重连（指数退避），按事件类型分发。
import { ElNotification } from 'element-plus'

let socket = null
let retryDelay = 1000
const listeners = new Map() // type -> Set<fn>

export function onEvent(type, fn) {
  if (!listeners.has(type)) listeners.set(type, new Set())
  listeners.get(type).add(fn)
  return () => listeners.get(type)?.delete(fn)
}

function dispatch(msg) {
  listeners.get(msg.type)?.forEach((fn) => {
    try { fn(msg.data, msg) } catch { /* 忽略单个监听器错误 */ }
  })
}

export function connectWs() {
  const token = localStorage.getItem('token')
  if (!token || (socket && socket.readyState <= WebSocket.OPEN)) return
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  socket = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`)
  socket.onopen = () => { retryDelay = 1000 }
  socket.onmessage = (e) => {
    let msg
    try { msg = JSON.parse(e.data) } catch { return }
    if (msg.type === 'ping') return
    dispatch(msg)
    if (msg.type === 'notify') {
      const d = msg.data
      ElNotification({
        title: d.title,
        message: [d.body, ...Object.entries(d.fields || {}).map(([k, v]) => `${k}: ${v}`)]
          .filter(Boolean).join('\n'),
        type: { info: 'info', warn: 'warning', error: 'error' }[d.level] || 'info',
        position: 'bottom-right',
        duration: 6000,
      })
    }
  }
  socket.onclose = () => {
    socket = null
    if (localStorage.getItem('token')) {
      setTimeout(connectWs, retryDelay)
      retryDelay = Math.min(retryDelay * 2, 30000)
    }
  }
  socket.onerror = () => socket?.close()
}

export function disconnectWs() {
  const s = socket
  socket = null
  s?.close()
}
