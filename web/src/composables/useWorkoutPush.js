import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { downloadFile, platName } from '../utils/common'

/**
 * 课表下发到手表平台（佳明/Strava）与 FIT 导出，总览页 / 计划页共用。
 * 连接状态每次下发时现查 /connections（连接可能在页面停留期间变化）；
 * 下发完成后回调 onRefresh，由页面决定刷新什么（总览刷聚合、计划页刷整页）。
 */
export function useWorkoutPush(onRefresh) {
  const pushingId = ref(null)

  async function pushWorkout(wo) {
    pushingId.value = wo.id
    try {
      // 只发支持下发写入的平台（高驰 MCP 未开放训练写入，带上必然失败一项）
      const conn = await api.get('/connections')
      const opts = conn.connections
        .filter(c => c.status === 'connected' && ['garmin', 'strava'].includes(c.platform))
        .map(c => c.platform)
      if (!opts.length) {
        ElMessage.warning('请先在「平台连接」页绑定佳明 / Strava（高驰暂未开放训练写入，可下载 FIT 手动导入）')
        return
      }
      // 已成功过的平台由后端按 pushed_platforms 幂等跳过
      const r = await api.post(`/plan/workouts/${wo.id}/push`, { platforms: opts })
      if (!r.pushed.length) {
        ElMessage.error(r.results?.filter(x => !x.ok).map(x => `${platName(x.platform)}：${x.error}`).join('；') || '下发失败')
      } else {
        ElMessage.success(`已下发：${r.pushed.map(platName).join('、')}，打开对应 App 同步到手表即可`)
      }
      await onRefresh()
    } catch (e) { ElMessage.error(e.message || '下发失败') } finally { pushingId.value = null }
  }

  async function downloadFit(wo) {
    await downloadFile(`/api/plan/workouts/${wo.id}/fit`, `workout_${wo.date}.fit`,
      { successTip: '已导出 FIT 文件，可在高驰/佳明 App 中导入' })
  }

  return { pushingId, pushWorkout, downloadFit }
}
