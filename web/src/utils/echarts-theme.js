/**
 * ECharts 暗色主题常量（全站统一，各页面不再各自复制 axisStyle/tooltipStyle）。
 * 配色与 main.js 的 CSS 变量体系保持一致。
 */
export const axisStyle = {
  axisLine: { lineStyle: { color: 'rgba(148,163,184,.2)' } },
  axisLabel: { color: '#5f6d80', fontSize: 11 },
}

export const tooltipStyle = {
  backgroundColor: '#1a2330',
  borderColor: 'rgba(148,163,184,.2)',
  textStyle: { color: '#e8eef6' },
}

/** 通用 splitLine 样式 */
export const splitLineStyle = { lineStyle: { color: 'rgba(148,163,184,.08)' } }

/** 通用 legend 样式 */
export const legendStyle = { textStyle: { color: '#9aa8ba' }, top: 0, itemWidth: 14 }
