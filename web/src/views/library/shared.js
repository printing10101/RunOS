/**
 * 知识库四个 Tab（方法库/计划匹配/智能推荐/原理）共用的展示映射与格式化。
 * 此前这些映射在各 Tab 各留一份副本，文案已经漂移（「坡地」vs「坡地跑」、
 * 「W1 30km」vs「W1:30km」）；收敛到这里，改文案只改一处。
 * 课型中文与后端 vocab.py、web/src/api.js 的 SESSION_STYLE 同源。
 */

export const LEVEL = { beginner: '入门', intermediate: '进阶', advanced: '高级', elite: '精英' }

export const WK = { mon: '一', tue: '二', wed: '三', thu: '四', fri: '五', sat: '六', sun: '日' }

// 方法库课表模板里的课型（含二代词汇 quality_1/threshold_am 等，比 api.js
// 的 SESSION_STYLE 覆盖面更大；基础课型两处标签必须一致）
export const SESSION = {
  easy: '轻松跑', easy_short: '短轻松跑', recovery: '恢复跑', recovery_cross: '恢复/交叉',
  tempo: '节奏跑', interval: '间歇跑', fartlek: '变速跑', hill: '坡地跑', long: '长距离',
  long_pace: '带配速长距离', long_with_mp: '马配长距离',
  medium_long: '中长距离', medium_long_or_LT: '中长距离/阈值', LT_or_VO2max: '阈值/VO2max',
  general_aerobic: '一般有氧', speed_sos: '速度 SOS', tempo_or_mp_sos: '节奏/马配 SOS',
  track_intervals: '跑道间歇', threshold_am: '阈值课(早)', threshold_pm: '阈值课(晚)',
  quality: '质量课', quality_1: '质量课 Q1', quality_2: '质量课 Q2',
  strength: '力量', cross: '交叉训练', race: '比赛', rest: '休息',
}

export const evTagType = lv => ({ A: 'success', B: 'primary', C: 'warning', D: 'info' }[lv] || 'info')

export const levelLabel = v => LEVEL[v] || v

export const sessionLabel = v => SESSION[v] || v

/** 周模板 → 「周一·轻松跑 · 周二·休息」一行；没有可展示的课返回空串 */
export function weekTemplateText(plan) {
  return Object.entries(plan?.week_template || {})
    .filter(([k, v]) => WK[k] && SESSION[v])
    .map(([k, v]) => `周${WK[k]}·${SESSION[v]}`)
    .join(' · ')
}

/** 长距离渐进 → 「W1 30km → W2 32km 备注」；无渐进返回空串 */
export function longRunText(plan) {
  const lr = plan?.long_run_progression || []
  return lr.map(x => `W${x.week} ${x.km}km${x.note ? ` ${x.note}` : ''}`).join(' → ')
}
