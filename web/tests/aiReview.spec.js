import { beforeEach, describe, expect, it, vi } from 'vitest'

const post = vi.fn()
const error = vi.fn()

// api 与 element-plus 都被 mock：本轮只验证组合式的状态机，
// 不验证真实网络与 toast 实现（那两处由 e2e 与人工点击覆盖）。
vi.mock('../src/api.js', () => ({ api: { post } }))
vi.mock('element-plus', () => ({ ElMessage: { error, warning: vi.fn(), success: vi.fn() } }))

const { useAiReview } = await import('../src/composables/aiReview.js')

/**
 * 后端五个解读端点（assessment / weekly-recap / diet / prediction / plan-review）
 * 契约一致：POST → { review, source }，可能很慢（本地模型），失败时后端已退规则版。
 * 这个组合式是全站解读页共用的唯一实现，锁住它的四条行为。
 */
describe('useAiReview', () => {
  beforeEach(() => {
    post.mockReset()
    error.mockReset()
  })

  it('正常返回时填 review 与 source，并复位 loading', async () => {
    post.mockResolvedValue({ review: '本期负荷偏高', source: 'llm' })
    const { review, reviewSource, reviewLoading, loadReview } = useAiReview('/api/x')

    await loadReview()
    expect(review.value).toBe('本期负荷偏高')
    expect(reviewSource.value).toBe('llm')
    expect(reviewLoading.value).toBe(false)
  })

  it('固定发空对象载荷：模板 @click 会把事件对象当首参传进来', async () => {
    post.mockResolvedValue({ review: '', source: '' })
    const { loadReview } = useAiReview('/api/x')

    // 模拟模板里的 @click="loadReview" —— Vue 会把 MouseEvent 传进来
    await loadReview({ isTrusted: true, type: 'click' })
    expect(post).toHaveBeenCalledWith('/api/x', {}, { timeout: 180000 })
  })

  it('guard 返回 false 时不采用本次结果，也不弹错误', async () => {
    post.mockResolvedValue({ ok: false, review: '不该显示', source: '' })
    const { review, loadReview } = useAiReview('/api/x', {
      guard: (r) => r.ok !== false,
    })

    await loadReview()
    expect(review.value).toBe('')
    expect(error).not.toHaveBeenCalled()
  })

  it('请求失败时弹错误提示、不把异常抛给调用方，且 loading 一定复位', async () => {
    post.mockRejectedValue(new Error('网络错误，无法连接到服务'))
    const { review, reviewLoading, loadReview } = useAiReview('/api/x')

    await expect(loadReview()).resolves.toBeUndefined()
    expect(review.value).toBe('')
    expect(error).toHaveBeenCalledWith('网络错误，无法连接到服务')
    expect(reviewLoading.value).toBe(false)
  })

  it('失败文案没有 message 时用 failTip 兜底', async () => {
    post.mockRejectedValue(new Error(''))
    const { loadReview } = useAiReview('/api/x', { failTip: '解读生成失败' })

    await loadReview()
    expect(error).toHaveBeenCalledWith('解读生成失败')
  })

  it('resetReview 清空文本与来源', async () => {
    post.mockResolvedValue({ review: 'abc', source: 'llm' })
    const { review, reviewSource, loadReview, resetReview } = useAiReview('/api/x')

    await loadReview()
    expect(review.value).toBe('abc')
    resetReview()
    expect(review.value).toBe('')
    expect(reviewSource.value).toBe('')
  })
})
