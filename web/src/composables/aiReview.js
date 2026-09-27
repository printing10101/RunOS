import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

/**
 * 后端五个解读端点（assessment / weekly-recap / diet / prediction / plan-review）
 * 共用的状态机：POST → { review, source }。本地模型可能很慢（超时给到 180s），
 * 失败时后端已退规则版，前端只负责展示与错误提示。
 */
export function useAiReview(url, { failTip = '解读生成失败', guard = null } = {}) {
  const review = ref('')
  const reviewSource = ref('')
  const reviewLoading = ref(false)

  async function loadReview(..._args) {
    // 不接收模板 @click 传进来的事件对象：载荷固定为空对象
    reviewLoading.value = true
    try {
      const r = await api.post(url, {}, { timeout: 180000 })
      if (guard && !guard(r)) return
      review.value = r.review || ''
      reviewSource.value = r.source || ''
    } catch (e) {
      ElMessage.error(e?.message || failTip)
    } finally {
      reviewLoading.value = false
    }
  }

  function resetReview() {
    review.value = ''
    reviewSource.value = ''
  }

  return { review, reviewSource, reviewLoading, loadReview, resetReview }
}
