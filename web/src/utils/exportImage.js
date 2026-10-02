import html2canvas from 'html2canvas'

/**
 * 把元素截图为 PNG 并触发浏览器下载，返回文件名。
 * 用于「本周复盘导出图片」这类分享出口：用户拿到的就是所见即所得的卡片。
 * 任何一步失败都向上抛（调用方负责提示），不做静默降级。
 *
 * @param {HTMLElement} el 要截图的元素
 * @param {string} filename 下载文件名
 */
export async function exportElementPng(el, filename) {
  if (!el) throw new Error('没有可导出的内容')
  const canvas = await html2canvas(el, {
    backgroundColor: '#081210',   // 暗色卡片脱离页面背景后需要显式底色
    scale: Math.min(2, window.devicePixelRatio || 1),
    logging: false,
  })
  const blob = await new Promise((resolve, reject) => canvas.toBlob(
    b => (b ? resolve(b) : reject(new Error('画布转 PNG 失败'))), 'image/png'))
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
  return filename
}
