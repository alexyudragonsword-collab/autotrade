export function fmt(n) {
  if (n == null) return '-'
  return Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

export function ts(s) {
  if (!s) return '-'
  return new Date(s.endsWith('Z') || s.includes('+') ? s : s + 'Z').toLocaleString('zh-CN', { hour12: false })
}

export function pct(x) {
  if (x == null) return '-'
  return (x * 100).toFixed(2) + '%'
}

/** 带 JWT 下载文件（axios 客户端只处理 JSON，二进制/附件走这里）。 */
export async function downloadFile(url, filename, params = {}) {
  const qs = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ''),
  ).toString()
  const resp = await fetch(qs ? `${url}?${qs}` : url, {
    headers: { Authorization: `Bearer ${localStorage.getItem('token')}` },
  })
  if (!resp.ok) throw new Error(`${resp.status}`)
  const blob = await resp.blob()
  const objectUrl = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = objectUrl
  a.download = filename
  a.click()
  URL.revokeObjectURL(objectUrl)
}

/** el-date-picker 的 daterange 值 → 后端 ISO 时刻参数（本地整日 → UTC）。 */
export function rangeParams(range) {
  if (!range || range.length !== 2 || !range[0] || !range[1]) return {}
  const from = new Date(range[0])
  from.setHours(0, 0, 0, 0)
  const to = new Date(range[1])
  to.setHours(23, 59, 59, 999)
  return { date_from: from.toISOString(), date_to: to.toISOString() }
}
