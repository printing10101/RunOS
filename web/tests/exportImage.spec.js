import { describe, expect, it, vi, beforeEach } from 'vitest'

const fakeCanvas = {
  toBlob: (cb) => cb({ size: 42, type: 'image/png' }),
}
vi.mock('html2canvas', () => ({
  default: vi.fn(async () => fakeCanvas),
}))

import html2canvas from 'html2canvas'
import { exportElementPng } from '../src/utils/exportImage'

describe('exportElementPng', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('URL', {
      createObjectURL: vi.fn(() => 'blob:x'),
      revokeObjectURL: vi.fn(),
    })
  })

  it('没有元素时直接报错而不是生成空文件', async () => {
    await expect(exportElementPng(null, 'a.png')).rejects.toThrow('没有可导出的内容')
  })

  it('截图 → PNG blob → 触发下载并回收 URL', async () => {
    const click = vi.fn()
    const makeLink = vi.spyOn(document, 'createElement').mockReturnValue({ click })
    const name = await exportElementPng(document.body, 'runos_weekly_recap_20261003.png')
    expect(name).toBe('runos_weekly_recap_20261003.png')
    expect(html2canvas).toHaveBeenCalledWith(document.body, expect.objectContaining({
      backgroundColor: '#081210',
    }))
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    expect(click).toHaveBeenCalledTimes(1)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:x')
    makeLink.mockRestore()
  })

  it('toBlob 给不出 blob 时响亮报错', async () => {
    vi.mocked(html2canvas).mockResolvedValueOnce({ toBlob: (cb) => cb(null) })
    await expect(exportElementPng(document.body, 'a.png')).rejects.toThrow('画布转 PNG 失败')
  })
})
