import { describe, expect, it } from 'vitest'
import { fmtDate, fmtPace, fmtTime, normalizeApiError } from '../src/api.js'

/**
 * api.js 是全站唯一的错误规范化出口：后端任何一个 4xx/5xx 的中文提示，
 * 都要经过它变成 Error.message 才展示给用户。这里锁三件事：
 *   1) 后端带 detail 时用后端的文案（不覆盖成泛化的"请求失败"）；
 *   2) 后端没带 detail 时按状态码给中文兜底，且 status 必须透传；
 *   3) 网络层异常（超时/断连）不依赖 response，单独识别。
 */

describe('normalizeApiError', () => {
  it('后端给了 detail 字符串时优先用它', () => {
    const e = normalizeApiError({ response: { status: 400, data: { detail: '目标日期必须在今天之后' } } })
    expect(e).toBeInstanceOf(Error)
    expect(e.message).toBe('目标日期必须在今天之后')
    expect(e.status).toBe(400)
  })

  it('detail 是对象时序列化，不丢信息', () => {
    const e = normalizeApiError({ response: { status: 422, data: { detail: { field: 'weight_kg' } } } })
    expect(e.message).toBe('{"field":"weight_kg"}')
    expect(e.status).toBe(422)
  })

  it('后端没给 detail 时按状态码兜底，且 status 透传', () => {
    const cases = [
      [403, '来源校验失败，请求被拒绝'],
      [404, '接口不存在'],
      [500, '服务器内部错误'],
      [502, '后端服务不可用'],
      [503, '后端服务暂不可用'],
    ]
    for (const [status, expected] of cases) {
      const e = normalizeApiError({ response: { status, data: {} } })
      expect(e.message).toBe(expected)
      expect(e.status).toBe(status)
    }
  })

  it('未知状态码也带得回数字，不落进泛化文案', () => {
    const e = normalizeApiError({ response: { status: 418, data: {} } })
    expect(e.message).toBe('请求失败（418）')
    expect(e.status).toBe(418)
  })

  it('保留原始 response，兼容旧调用点的 e.response?.data?.detail', () => {
    const resp = { status: 400, data: { detail: 'x' } }
    expect(normalizeApiError({ response: resp }).response).toBe(resp)
  })

  it('超时与断连没有 response，按 code 单独识别', () => {
    const timeout = normalizeApiError({ code: 'ETIMEDOUT' })
    expect(timeout.message).toMatch(/请求超时/)
    expect(timeout.status).toBe(0)

    const net = normalizeApiError({ code: 'ERR_NETWORK' })
    expect(net.message).toMatch(/网络错误/)
  })

  it('非 axios 错误原样返回，不吞掉原始信息', () => {
    const orig = new Error('boom')
    expect(normalizeApiError(orig)).toBe(orig)
  })
})

describe('展示格式化', () => {
  it('fmtTime：0 要显示成 0:00，空值才是 -', () => {
    expect(fmtTime(0)).toBe('0:00')
    expect(fmtTime(null)).toBe('-')
    expect(fmtTime(undefined)).toBe('-')
    expect(fmtTime(125)).toBe('2:05')
    expect(fmtTime(3661)).toBe('1:01:01')
  })

  it('fmtPace：秒/公里转 m:ss/km', () => {
    expect(fmtPace(0)).toBe('-')
    expect(fmtPace(300)).toBe('5:00/km')
    expect(fmtPace(299.6)).toBe('5:00/km')
  })

  it('fmtDate：只取月/日', () => {
    expect(fmtDate('2026-09-14')).toBe('09/14')
    expect(fmtDate(undefined)).toBe('')
  })
})
