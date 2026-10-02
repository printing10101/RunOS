import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { printElement } from '../src/utils/exportPrint'

describe('printElement', () => {
  beforeEach(() => {
    document.head.innerHTML = ''
    document.body.innerHTML = `
      <div><header>顶栏</header><section class="recap-card"><p>复盘内容</p></section><footer>其他</footer></div>`
    vi.stubGlobal('print', vi.fn())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.classList.remove('printing-zoom')
  })

  it('没有目标时直接报错', () => {
    expect(() => printElement('#nope')).toThrow('没有可打印的内容')
  })

  it('挂 print-target 类、调起打印，样式表已注入', () => {
    printElement(document.querySelector('.recap-card'))
    expect(window.print).toHaveBeenCalledTimes(1)
    expect(document.body.classList.contains('printing-zoom')).toBe(true)
    const css = document.head.querySelector('style')?.textContent || ''
    expect(css).toContain('@media print')
    expect(css).toMatch(/\.recap-card/)
    expect(css).toContain('.el-button { display: none')
  })

  it('afterprint 后自动摘类并解绑监听', () => {
    printElement(document.querySelector('.recap-card'))
    expect(document.body.classList.contains('printing-zoom')).toBe(true)
    window.dispatchEvent(new Event('afterprint'))
    expect(document.body.classList.contains('printing-zoom')).toBe(false)
  })

  it('手动 cleanup 同样摘类（afterprint 不触发的环境兜底）', () => {
    const off = printElement(document.querySelector('.recap-card'))
    off()
    expect(document.body.classList.contains('printing-zoom')).toBe(false)
  })
})
