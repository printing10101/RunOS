import axios from 'axios'

const http = axios.create({ baseURL: '/api', timeout: 30000 })

// 把 axios 错误规范化为带 status 与可读中文 message 的 Error，
// 供调用方 catch，或进入 main.js 的全局兜底提示。
export function normalizeApiError(err) {
  if (err.response) {
    const { status, data } = err.response
    const detail = data && (data.detail ?? data.message)
    let text = ''
    if (typeof detail === 'string') text = detail
    else if (detail) {
      try { text = JSON.stringify(detail) } catch { text = String(detail) }
    }
    const fallback = {
      400: '请求无效',
      401: '未登录或凭证失效',
      403: '来源校验失败，请求被拒绝',
      404: '接口不存在',
      422: '参数校验失败',
      429: '请求过于频繁，请稍后再试',
      500: '服务器内部错误',
      502: '后端服务不可用',
      503: '后端服务暂不可用',
      504: '后端服务响应超时',
    }[status] || `请求失败（${status}）`
    const e = new Error(text || fallback)
    e.status = status
    // 保留原始 response，兼容既有调用点的 e.response?.data?.detail 写法
    e.response = err.response
    return e
  }
  if (err.code === 'ECONNABORTED' || err.code === 'ETIMEDOUT') {
    return Object.assign(new Error('请求超时，请确认后端服务正在运行'), { status: 0 })
  }
  if (err.code === 'ERR_NETWORK') {
    return Object.assign(new Error('网络错误，无法连接到服务'), { status: 0 })
  }
  return err
}

http.interceptors.response.use(
  r => r,
  err => Promise.reject(normalizeApiError(err)),
)

export const api = {
  get: (url, params, config) => http.get(url, { params, ...config }).then(r => r.data),
  post: (url, body, config) => http.post(url, body, config).then(r => r.data),
  put: (url, body, config) => http.put(url, body, config).then(r => r.data),
  del: (url, config) => http.delete(url, config).then(r => r.data),
}

export function fmtTime(sec) {
  if (!sec && sec !== 0) return '-'
  const t = Math.round(sec)
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  const s = t % 60
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`
}

export function fmtPace(secPerKm) {
  if (!secPerKm) return '-'
  // 先四舍五入到整秒再拆分：逐分量取整会把 299.6 拼成 "4:60/km"
  const t = Math.round(secPerKm)
  const m = Math.floor(t / 60)
  const s = t % 60
  return `${m}:${String(s).padStart(2, '0')}/km`
}

export function fmtDate(d) {
  return d ? String(d).slice(5, 10).replace('-', '/') : ''
}

export const SESSION_STYLE = {
  easy: { label: '轻松跑', color: '#5fc987' },
  quality: { label: '质量课', color: '#e05f5f' },
  long: { label: '长距离', color: '#d9a24e' },
  strength: { label: '力量', color: '#5f9fc9' },
  core: { label: '核心', color: '#7d938c' },
  rest: { label: '休息', color: '#5c6f68' },
  cross: { label: '交叉', color: '#4fc3c7' },
  // 知识库体验周引入的课型（routers/methods 生成）
  fartlek: { label: '变速跑', color: '#d98f4a' },
  hill: { label: '坡地跑', color: '#d4b06a' },
  tempo: { label: '节奏跑', color: '#d98f4a' },
  interval: { label: '间歇跑', color: '#e05f5f' },
  recovery: { label: '恢复跑', color: '#5fc987' },
  race: { label: '比赛', color: '#e05f5f' },
}
