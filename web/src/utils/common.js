import { ElMessage } from 'element-plus'

/**
 * 本地时区的「今天」YYYY-MM-DD。
 * 不要用 toISOString().slice(0,10) 取日期，那是 UTC 日期，
 * 东八区 0-8 点会拿到昨天。
 */
export function todayStr() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** Date → 'YYYY-MM-DD'（本地时区） */
export function localDateStr(d) {
  const t = d instanceof Date ? d : new Date(d)
  return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')}`
}

/** Date → 'YYYY-MM-DD HH:mm:ss'（本地时区，供 datetime picker value-format 用） */
export function localDateTimeStr(d) {
  const t = d instanceof Date ? d : new Date(d)
  const p = (n) => String(n).padStart(2, '0')
  return `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())} ${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}`
}

/**
 * 'YYYY-MM-DD'（或带时间的串）→ 本地时区 Date。
 * 不要写 new Date('2026-09-14')：纯日期串按 ISO 规则被当作 UTC 午夜，
 * 在东八区虽恰好同日，但 getDay()/getDate() 在西半球时区会整体差一天。
 */
export function parseLocalDate(s) {
  const [y, m, d] = String(s).split('-').map(Number)
  return new Date(y, m - 1, d)
}

/** 两个 'YYYY-MM-DD'（或 Date）之间相差的整天数，按本地日历日计算 */
export function daysBetween(a, b) {
  const da = a instanceof Date ? a : parseLocalDate(a)
  const db = b instanceof Date ? b : parseLocalDate(b)
  return Math.round((db - da) / 86400000)
}

/** 全站共享的展示工具：平台/阶段/等级命名与配色、文件下载 */

export const PLATFORM_NAMES = {
  garmin: '佳明',
  coros: '高驰',
  strava: 'Strava',
  manual: '手动录入',
}
export function platName(p) { return PLATFORM_NAMES[p] || p }

export const PHASE_NAMES = { base: '基础期', build: '强化期', peak: '巅峰期', taper: '减量期' }
export const PHASE_COLORS = { base: '#5fc987', build: '#5f9fc9', peak: '#e05f5f', taper: '#d9a24e' }
export function phaseName(p) { return PHASE_NAMES[p] || p }
export function phaseColor(p) { return PHASE_COLORS[p] || '#7d938c' }

export const GRADE_COLORS = { S: '#d4b06a', A: '#3fd0a4', B: '#5f9fc9', C: '#7d938c', D: '#5c6f68' }
export function gradeColor(g) { return GRADE_COLORS[g] || '#7d938c' }

export const STEP_COLORS = {
  warmup: '#4fc3c7', active: '#e05f5f', rest: '#7d938c', cooldown: '#4fc3c7', strength: '#5f9fc9',
}
export function stepColor(t) { return STEP_COLORS[t] || '#7d938c' }

export const KIND_COLORS = { race: '#d4b06a', quality: '#e05f5f', long: '#d9a24e', easy: '#5fc987', run: '#5fc987' }
export function kindColor(k) { return KIND_COLORS[k] || '#5f9fc9' }

/**
 * 下载二进制文件（blob）。统一 resp.ok 检查，避免失败时静默保存损坏文件。
 * @throws 下载失败时抛出 Error（调用方无需重复提示，本函数已弹 toast）
 */
export async function downloadFile(url, filename, { successTip, failTip } = {}) {
  try {
    const resp = await fetch(url)
    if (!resp.ok) {
      const msg = failTip || `下载失败（HTTP ${resp.status}）`
      ElMessage.warning(msg)
      return false
    }
    const blob = await resp.blob()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = filename
    a.click()
    URL.revokeObjectURL(a.href)
    if (successTip) ElMessage.success(successTip)
    return true
  } catch {
    ElMessage.error('导出失败，请确认后端服务正在运行')
    return false
  }
}

// ACWR 配色按训练学安全线分区（与后端 gear 防伤/状态页阈值同源）：
// 0.8–1.3 达标；1.3–1.5 偏高；>1.5 高风险；<0.8 负荷不足；无数据中性灰。
export function acwrColor(v, fallback = '#5c6f68') {
  if (v == null || isNaN(v)) return fallback
  if (v > 1.5) return '#e05f5f'
  if (v > 1.3) return '#d9a24e'
  if (v >= 0.8) return '#5fc987'
  return '#5f9fc9'
}
