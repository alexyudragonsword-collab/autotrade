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
