/**
 * ECharts 暗色主题常量（全站统一，各页面不再各自复制 axisStyle/tooltipStyle）。
 * 配色与 main.js 的 CSS 变量体系保持一致。
 */
export const axisStyle = {
  axisLine: { lineStyle: { color: 'rgba(157,184,173,.2)' } },
  axisLabel: { color: '#5c6f68', fontSize: 11 },
}

export const tooltipStyle = {
  backgroundColor: '#14251f',
  borderColor: 'rgba(157,184,173,.2)',
  textStyle: { color: '#dce8e2' },
}

/** 通用 splitLine 样式 */
export const splitLineStyle = { lineStyle: { color: 'rgba(157,184,173,.08)' } }

/** 通用 legend 样式 */
export const legendStyle = { textStyle: { color: '#8fada2' }, top: 0, itemWidth: 14 }
