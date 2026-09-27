import { describe, expect, it } from 'vitest'
import {
  acwrColor,
  daysBetween,
  gradeColor,
  kindColor,
  localDateStr,
  localDateTimeStr,
  parseLocalDate,
  phaseColor,
  phaseName,
  platName,
  stepColor,
  todayStr,
} from '../src/utils/common.js'

/**
 * 只测纯逻辑，不测 DOM。重点锁两类容易回归的东西：
 *   1) 时区口径——本地时区取日期，不能用 toISOString，否则东八区 0-8 点拿到昨天；
 *   2) 映射函数的兜底——未知 key 要回落到可辨识的值，不能变 undefined 让模板渲染空白。
 */

describe('日期工具（本地时区口径）', () => {
  it('parseLocalDate 按本地日历日解析，不按 UTC', () => {
    const d = parseLocalDate('2026-09-14')
    expect(d.getFullYear()).toBe(2026)
    expect(d.getMonth()).toBe(8) // 0-based
    expect(d.getDate()).toBe(14)
    // 若被当成 UTC 午夜再转本地，东八区仍是 14 日，但西半球会差一天；
    // 这里用 getDay 锁定本地语义：2026-09-14 是周一
    expect(d.getDay()).toBe(1)
  })

  it('localDateStr 与 parseLocalDate 可往返', () => {
    expect(localDateStr(parseLocalDate('2026-03-01'))).toBe('2026-03-01')
  })

  it('localDateTimeStr 补零到两位', () => {
    const s = localDateTimeStr(new Date(2026, 8, 14, 9, 5, 3))
    expect(s).toBe('2026-09-14 09:05:03')
  })

  it('todayStr 是本地 YYYY-MM-DD，且与 localDateStr(now) 一致', () => {
    expect(todayStr()).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    expect(todayStr()).toBe(localDateStr(new Date()))
  })

  it('daysBetween 按整天数计算，跨月跨年正确', () => {
    expect(daysBetween('2026-09-14', '2026-09-16')).toBe(2)
    expect(daysBetween('2026-08-31', '2026-09-01')).toBe(1)
    expect(daysBetween('2025-12-31', '2026-01-01')).toBe(1)
    // 反序应给负数，而不是 0
    expect(daysBetween('2026-09-16', '2026-09-14')).toBe(-2)
  })
})

describe('展示映射的兜底值', () => {
  it('平台/阶段名未知 key 原样回落，不给 undefined', () => {
    expect(platName('coros')).toBe('高驰')
    expect(platName('unknown')).toBe('unknown')
    expect(phaseName('taper')).toBe('减量期')
    expect(phaseName('nope')).toBe('nope')
  })

  it('取色函数未知 key 回落灰色而不是 undefined', () => {
    expect(phaseColor('build')).toBe('#5f9fc9')
    expect(phaseColor('nope')).toBe('#7d938c')
    expect(gradeColor('S')).toBe('#d4b06a')
    expect(gradeColor('Z')).toBe('#7d938c')
    expect(stepColor('warmup')).toBe('#4fc3c7')
    expect(stepColor('nope')).toBe('#7d938c')
    expect(kindColor('quality')).toBe('#e05f5f')
    expect(kindColor('nope')).toBe('#5f9fc9')
  })

  it('acwrColor 按训练学安全线分区（与后端 review_guard 同源阈值）', () => {
    expect(acwrColor(null)).toBe('#5c6f68') // 无数据走中性灰
    expect(acwrColor(1.0)).toBe('#5fc987') // 0.8–1.3 达标
    expect(acwrColor(0.8)).toBe('#5fc987')
    expect(acwrColor(1.3)).toBe('#5fc987')
    expect(acwrColor(1.4)).toBe('#d9a24e') // 1.3–1.5 偏高
    expect(acwrColor(1.6)).toBe('#e05f5f') // >1.5 高风险
  })
})
