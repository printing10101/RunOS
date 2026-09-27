import * as echarts from 'echarts'

/**
 * 统一管理一批 ECharts 实例：
 * - init 去重（同名/同 DOM 复用实例）
 * - 窗口缩放时自动 resize（此前全站图表均不随窗口重排）
 * - disposeAll 一次性释放，组件 onUnmounted 调用
 */
export function createChartManager() {
  const map = new Map()
  const onResize = () => map.forEach(c => { try { c.resize() } catch { /* 已释放则忽略 */ } })
  window.addEventListener('resize', onResize)

  function get(name, dom) {
    if (!map.has(name)) map.set(name, echarts.init(dom))
    return map.get(name)
  }

  /** 释放全部实例但保留 resize 监听（用于数据变化后全量重绘） */
  function clear() {
    map.forEach(c => { try { c.dispose() } catch { /* ignore */ } })
    map.clear()
  }

  /** 组件卸载时调用：释放实例并解除 resize 监听 */
  function disposeAll() {
    clear()
    window.removeEventListener('resize', onResize)
  }

  return { get, clear, disposeAll }
}
