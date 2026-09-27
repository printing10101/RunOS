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
export const PHASE_COLORS = { base: '#4ade80', build: '#5b9dff', peak: '#ff6b6b', taper: '#ffa24d' }
export function phaseName(p) { return PHASE_NAMES[p] || p }
export function phaseColor(p) { return PHASE_COLORS[p] || '#7d8ea3' }

export const GRADE_COLORS = { S: '#f5c26b', A: '#c8f169', B: '#5b9dff', C: '#7d8ea3', D: '#5f6d80' }
export function gradeColor(g) { return GRADE_COLORS[g] || '#7d8ea3' }

export const STEP_COLORS = {
  warmup: '#56d4e0', active: '#ff6b6b', rest: '#7d8ea3', cooldown: '#56d4e0', strength: '#5b9dff',
}
export function stepColor(t) { return STEP_COLORS[t] || '#7d8ea3' }

export const KIND_COLORS = { race: '#f5c26b', quality: '#ff6b6b', long: '#ffa24d', easy: '#4ade80', run: '#4ade80' }
export function kindColor(k) { return KIND_COLORS[k] || '#5b9dff' }

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
export function acwrColor(v, fallback = '#5f6d80') {
  if (v == null || isNaN(v)) return fallback
  if (v > 1.5) return '#ff6b6b'
  if (v > 1.3) return '#ffa24d'
  if (v >= 0.8) return '#4ade80'
  return '#5b9dff'
}
