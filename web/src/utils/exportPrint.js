/**
 * 「只打印这一块」的导出出口：给 body 挂 print-target 类后调起系统打印，
 * 用户在打印预览里「另存为 PDF」即得分享文件。
 *
 * 为什么不是 html2canvas 截图：实测它在 RunOS 的重装饰页面上同步渲染
 * 死循环（主线程卡死分钟级），库级问题无解；浏览器原生打印管线确定性强、
 * 零依赖，PDF 也更适合分享与存档。
 *
 * 用法：
 *   const off = printElement('.recap-card')   // 传目标元素或选择器
 *   // afterprint 时自动摘类；需要手动清理时调返回的 off()
 */

/** @type {HTMLStyleElement | null} 打印期专用的动态样式（隐藏目标以外一切） */
let styleEl = null

export function printElement(target) {
  const el = typeof target === 'string' ? document.querySelector(target) : target
  if (!el) throw new Error('没有可打印的内容')
  if (!styleEl) {
    styleEl = document.createElement('style')
    document.head.appendChild(styleEl)
  }
  // visibility 方案保留布局，卡片位置不动；配色翻转为打印友好的白底黑字
  styleEl.textContent = `
    @media print {
      @page { margin: 12mm; }
      body.printing-zoom { background: #fff !important; }
      body.printing-zoom * { visibility: hidden !important; }
      body.printing-zoom ${selectorOf(el)}, body.printing-zoom ${selectorOf(el)} * {
        visibility: visible !important;
        color: #1a2b25 !important;
        background-image: none !important;
        box-shadow: none !important;
        text-shadow: none !important;
      }
      body.printing-zoom ${selectorOf(el)} {
        position: absolute !important;
        left: 0; top: 0; width: 100%;
        background: #fff !important;
        border: none !important;
        backdrop-filter: none !important;
      }
      body.printing-zoom ${selectorOf(el)} .el-button { display: none !important; }
    }`
  document.body.classList.add('printing-zoom')
  const cleanup = () => {
    document.body.classList.remove('printing-zoom')
    window.removeEventListener('afterprint', cleanup)
  }
  window.addEventListener('afterprint', cleanup)
  window.print()
  return cleanup
}

function selectorOf(el) {
  // 目标卡片由 Vue 渲染：类名优先（scoped 类在单卡片场景足够稳定），无类再退 nth-of-type
  const cls = [...el.classList].map(c => `.${c}`).join('')
  if (cls) return `${el.tagName.toLowerCase()}${cls}`
  const parent = el.parentElement
  if (!parent) return el.tagName.toLowerCase()
  const sameTag = [...parent.children].filter(c => c.tagName === el.tagName)
  const idx = sameTag.indexOf(el) + 1
  return `${parent.tagName.toLowerCase()} > ${el.tagName.toLowerCase()}:nth-of-type(${idx})`
}
