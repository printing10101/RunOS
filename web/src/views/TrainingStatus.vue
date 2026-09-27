<template>
  <el-skeleton v-if="loading" style="margin-top:8px" :rows="9" animated />
  <el-empty v-else-if="loadError" :description="loadError">
    <el-button type="primary" round @click="loadAll">重试</el-button>
  </el-empty>
  <div v-else-if="!d.empty">
    <el-row :gutter="14">
      <el-col :span="10">
        <el-card shadow="never" class="status-card" :class="'st-' + d.status?.key">
          <template #header><div class="card-head">当前训练状态</div></template>
          <div class="status-label num-display">{{ d.status?.label }}</div>
          <div class="status-detail">{{ d.status?.detail }}</div>
          <div class="status-trend" v-if="d.vdot_trend != null">近 16 周 VDOT 趋势 {{ d.vdot_trend > 0 ? '+' : '' }}{{ d.vdot_trend }}</div>
        </el-card>
      </el-col>
      <el-col :span="7">
        <el-card shadow="never">
          <template #header><div class="card-head">训练准备度</div></template>
          <div class="ready-wrap">
            <RingGauge :value="d.readiness?.score || 0" :label="d.readiness?.score ?? '-'" sub="今日可练度"
                  :sub2="d.readiness?.score >= 80 ? '绿灯' : d.readiness?.score >= 60 ? '黄灯' : '红灯'"
                  :color="readyColor" size="120" />
          </div>
          <div class="ready-verdict">{{ d.readiness?.verdict }}</div>
        </el-card>
      </el-col>
      <el-col :span="7">
        <el-card shadow="never">
          <template #header><div class="card-head">恢复 · 负荷比</div></template>
          <div class="rec-row"><span class="rec-k">恢复时间</span><span class="num-display rec-v">{{ d.recovery_time_h }}<span class="rec-u">h</span></span></div>
          <div class="rec-row"><span class="rec-k">急慢性负荷比 ACWR</span>
            <span class="num-display rec-v" :style="{ color: acwrColor }">{{ d.acwr ?? '-' }}</span></div>
          <div class="rec-note">ACWR 安全区 0.8 – 1.3；>1.5 伤病风险显著上升</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="14" style="margin-top:14px">
      <el-col :span="16">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">每日训练负荷（近 12 周）
              <span class="card-sub">7 天均 {{ d.fatigue }} · 28 天均 {{ d.fitness }} · 形态 {{ d.form > 0 ? '+' : '' }}{{ d.form }}</span>
            </div>
          </template>
          <div ref="loadChart" style="height: 280px"></div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never">
          <template #header><div class="card-head">负荷重点 <span class="card-sub">近 28 天</span></div></template>
          <template v-if="d.load_focus?.available">
            <div class="focus-bar">
              <div class="fb-seg" :style="{ width: d.load_focus.pct.low_aerobic + '%', background: '#4ade80' }"></div>
              <div class="fb-seg" :style="{ width: d.load_focus.pct.high_aerobic + '%', background: '#ffa24d' }"></div>
              <div class="fb-seg" :style="{ width: d.load_focus.pct.anaerobic + '%', background: '#ff6b6b' }"></div>
            </div>
            <div class="focus-pcts">
              <span style="color:#4ade80">{{ d.load_focus.pct.low_aerobic }}%</span>
              <span style="color:#ffa24d">{{ d.load_focus.pct.high_aerobic }}%</span>
              <span style="color:#ff6b6b">{{ d.load_focus.pct.anaerobic }}%</span>
            </div>
            <div class="focus-verdict">{{ d.load_focus.verdict }}</div>
            <div class="focus-guide">
              <div v-for="g in focusGuide" :key="g.label" class="fg-row">
                <span class="dot" :style="{ background: g.color }"></span>{{ g.label }}
                <span class="fg-range">参考 {{ g.range }}</span>
              </div>
              <div v-if="d.load_focus.strength_hours" class="fg-row">▣ 力量 {{ d.load_focus.strength_hours }}h（不计入占比）</div>
            </div>
          </template>
          <el-empty v-else :description="d.load_focus?.verdict || '暂无数据'" :image-size="60" />
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="14" style="margin-top:14px">
      <el-col :span="8">
        <el-card shadow="never">
          <template #header><div class="card-head">单调性 · 应变 <span class="card-sub">Foster 体系</span></div></template>
          <template v-if="d.monotony?.monotony != null">
            <div class="mono-row">
              <div class="mono-item">
                <div class="num-display mono-num" :style="{ color: monoColor }">{{ d.monotony.monotony }}</div>
                <div class="mono-label">单调性（&lt;1.5 良好 · &gt;2.0 风险）</div>
              </div>
              <div class="mono-item">
                <div class="num-display mono-num">{{ d.monotony.strain ?? '-' }}</div>
                <div class="mono-label">周应变 = 周负荷 × 单调性</div>
              </div>
            </div>
            <div class="mono-verdict">{{ d.monotony.verdict }}</div>
          </template>
          <el-empty v-else :description="d.monotony?.verdict || '暂无数据'" :image-size="60" />
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never">
          <template #header><div class="card-head">有氧效率 <span class="card-sub">EF · 近 8 周低强度心率跑</span></div></template>
          <template v-if="d.aerobic_efficiency?.available">
            <div class="mono-row">
              <div class="mono-item">
                <div class="num-display mono-num" style="color: var(--cyan)">{{ d.aerobic_efficiency.ef_recent }}</div>
                <div class="mono-label">近 8 周 EF</div>
              </div>
              <div class="mono-item">
                <div class="num-display mono-num" :style="{ color: efColor }">
                  {{ d.aerobic_efficiency.delta_pct == null ? '-' : (d.aerobic_efficiency.delta_pct > 0 ? '+' : '') + d.aerobic_efficiency.delta_pct + '%' }}
                </div>
                <div class="mono-label">对比前 8 周</div>
              </div>
            </div>
            <div class="mono-verdict">{{ d.aerobic_efficiency.verdict }}</div>
          </template>
          <el-empty v-else :description="d.aerobic_efficiency?.verdict || '暂无数据'" :image-size="60" />
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never">
          <template #header><div class="card-head">前瞻负荷 <span class="card-sub">按当前计划推演</span></div></template>
          <template v-if="fc.verdict">
            <div class="mono-row" v-if="fc.race">
              <div class="mono-item">
                <div class="num-display mono-num" :style="{ color: raceFormColor }">{{ fc.race.tsb_on_race > 0 ? '+' : '' }}{{ fc.race.tsb_on_race }}</div>
                <div class="mono-label">比赛日形态（{{ fc.race.days_to_race }} 天后）</div>
              </div>
            </div>
            <div class="mono-verdict">{{ fc.verdict }}</div>
            <div class="mono-warn" v-for="(w, i) in fc.warnings || []" :key="i">⚠ {{ w }}</div>
          </template>
          <el-empty v-else :description="fc.reason || fc.verdict || '生成训练计划后可预览未来 12 周体能/疲劳/形态曲线'" :image-size="60" />
        </el-card>
      </el-col>
    </el-row>

    <el-row v-if="fc.series?.length" :gutter="14" style="margin-top:14px">
      <el-col :span="24">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">前瞻负荷规划（CTL 体能 / ATL 疲劳 / TSB 形态 · 未来 12 周）
              <span class="card-sub">未来负荷按课型由引擎估算</span>
            </div>
          </template>
          <div ref="fcChart" style="height: 300px"></div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="14" style="margin-top:14px">
      <el-col :span="12" v-for="ins in insightCards" :key="ins.key">
        <el-card shadow="never">
          <template #header><div class="card-head">{{ ins.title }} <span class="card-sub">{{ ins.sub }}</span></div></template>
          <template v-if="ins.data?.available">
            <div class="mono-row">
              <div class="mono-item">
                <div class="num-display mono-num" :style="{ color: insVerdictColor(ins.data) }">{{ ins.data.display || ins.data.value }}</div>
                <div class="mono-label">{{ ins.data.verdict_label }}</div>
              </div>
            </div>
            <div class="mono-verdict">{{ ins.data.why }}</div>
            <div class="mono-warn" v-for="(e, i) in (ins.data.evidence || []).slice(0, 2)" :key="i">{{ e }}</div>
            <div class="mono-verdict" v-for="(a, i) in (ins.data.actions || []).slice(0, 2)" :key="'a' + i">→ {{ a }}</div>
          </template>
          <el-empty v-else :description="ins.data?.unavailable_reason || '数据积累中'" :image-size="60" />
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="14" style="margin-top:14px">
      <el-col :span="8">
        <el-card shadow="never" class="score-card">
          <template #header><div class="card-head">耐力得分 <span class="card-sub">工程估计</span></div></template>
          <div class="score-num num-display" style="color: var(--lime)">{{ d.endurance_score?.score ?? '-' }}</div>
          <div class="score-label">{{ d.endurance_score?.label }}</div>
          <div class="score-detail">{{ d.endurance_score?.detail }}</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" class="score-card">
          <template #header><div class="card-head">爬坡得分 <span class="card-sub">工程估计</span></div></template>
          <div class="score-num num-display" style="color: var(--orange)">{{ d.hill_score?.score ?? '-' }}</div>
          <div class="score-label">{{ d.hill_score?.label }}</div>
          <div class="score-detail">{{ d.hill_score?.detail }}</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never">
          <template #header><div class="card-head">准备度构成</div></template>
          <div v-for="c in d.readiness?.components || []" :key="c.name" class="comp-row">
            <div class="comp-head"><span>{{ c.name }}</span><span class="comp-score">{{ c.score }}</span></div>
            <el-progress :percentage="c.score" :show-text="false" :stroke-width="6"
                         :color="c.score >= 75 ? '#4ade80' : c.score >= 50 ? '#ffa24d' : '#ff6b6b'" />
            <div class="comp-detail">{{ c.detail }}</div>
          </div>
          <div v-if="!d.readiness?.components?.length" class="rec-note">接入佳明或手动录入身体数据后启用睡眠/HRV 分量</div>
        </el-card>
      </el-col>
    </el-row>
  </div>
  <el-empty v-else description="请先完善档案并同步训练数据" />
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import * as echarts from 'echarts'
import { api } from '../api'
import RingGauge from '../components/RingGauge.vue'
import { axisStyle, tooltipStyle, splitLineStyle, legendStyle } from '../utils/echarts-theme'
import { createChartManager } from '../composables/charts'

const d = ref({ empty: true })
const fc = ref({})
const pi = ref({})          // /training-status/pro-insights：解耦 / 疲劳抗性等进阶洞察
const loadError = ref('')
const loading = ref(true)
const loadChart = ref(null)
const fcChart = ref(null)
// 统一管理图表实例：init 去重、窗口缩放自动 resize、卸载时释放
const charts = createChartManager()

const insightCards = computed(() => [
  { key: 'decoupling', title: '耐力稳定性（心率解耦）', sub: '前/后半程 EF 漂移 · 近 8 周', data: pi.value.insights?.find(i => i.key === 'decoupling') },
  { key: 'fatigue_resistance', title: '疲劳抗性', sub: '长距离后半程衰减合成', data: pi.value.insights?.find(i => i.key === 'fatigue_resistance') },
])

function insVerdictColor(data) {
  const v = data?.verdict
  return v === 'good' ? 'var(--lime)' : v === 'watch' ? 'var(--orange)' : 'var(--red)'
}

async function loadAll() {
  loading.value = true
  loadError.value = ''
  try {
    d.value = await api.get('/training-status')
    if (d.value.empty) return
    fc.value = await api.get('/training-status/forecast').catch(() => ({}))
    pi.value = await api.get('/training-status/pro-insights').catch(() => ({}))
    loading.value = false   // 图表容器在 v-else-if 分支里，必须先撤骨架屏才进 DOM
    await nextTick()        // 等 v-if 包裹的容器挂载后再 init，否则 echarts.init(null)
    renderCharts()
  } catch (e) {
    loadError.value = e.message || '加载失败，请确认后端服务正在运行'
    loading.value = false
  }
}

const readyColor = computed(() => {
  const s = d.value.readiness?.score || 0
  return s >= 80 ? '#4ade80' : s >= 60 ? '#ffa24d' : '#ff6b6b'
})
const acwrColor = computed(() => {
  const v = d.value.acwr
  if (v == null) return '#e8eef6'
  return v >= 0.8 && v <= 1.3 ? '#4ade80' : v > 1.5 ? '#ff6b6b' : '#ffa24d'
})
const monoColor = computed(() => {
  const v = d.value.monotony?.monotony
  if (v == null) return '#e8eef6'
  return v <= 1.5 ? '#4ade80' : v <= 2.0 ? '#ffa24d' : '#ff6b6b'
})
const efColor = computed(() => {
  const v = d.value.aerobic_efficiency?.delta_pct
  if (v == null) return '#e8eef6'
  return v >= 1.5 ? '#4ade80' : v <= -1.5 ? '#ff6b6b' : '#5b9dff'
})
const raceFormColor = computed(() => {
  const v = fc.value.race?.tsb_on_race
  if (v == null) return '#e8eef6'
  return v >= -15 && v <= 10 ? '#4ade80' : v < -15 ? '#ff6b6b' : '#ffa24d'
})
const focusGuide = computed(() => {
  const f = d.value.load_focus || {}
  return [
    { label: '低强度有氧', color: '#4ade80', range: `${f.guide?.low_aerobic?.[0]}-${f.guide?.low_aerobic?.[1]}%` },
    { label: '高强度有氧', color: '#ffa24d', range: `${f.guide?.high_aerobic?.[0]}-${f.guide?.high_aerobic?.[1]}%` },
    { label: '无氧', color: '#ff6b6b', range: `≤${f.guide?.anaerobic?.[1]}%` },
  ]
})

function renderCharts() {
  const days = d.value.daily || []
  charts.get('load', loadChart.value).setOption({
    grid: { left: 46, right: 16, top: 30, bottom: 26 },
    legend: legendStyle,
    tooltip: { trigger: 'axis', ...tooltipStyle },
    xAxis: { type: 'category', data: days.map(x => x.date.slice(5)), ...axisStyle,
      axisLabel: { ...axisStyle.axisLabel, interval: 13 } },
    yAxis: { type: 'value', ...axisStyle, splitLine: splitLineStyle },
    series: [
      { name: '当日负荷', type: 'bar', data: days.map(x => x.load), barWidth: '62%',
        itemStyle: { borderRadius: [3, 3, 0, 0],
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(91,157,255,.85)' }, { offset: 1, color: 'rgba(91,157,255,.15)' }]) } },
      { name: '7 天均值', type: 'line', symbol: 'none', smooth: true, data: rolling(days, 7),
        lineStyle: { color: '#ff6b6b', width: 2 } },
      { name: '28 天均值', type: 'line', symbol: 'none', smooth: true, data: rolling(days, 28),
        lineStyle: { color: '#c8f169', width: 2 } },
    ],
  })

  // 前瞻负荷规划图：实际/计划负荷柱 + CTL/ATL/TSB 曲线
  if (fc.value.series?.length) {
    const s = fc.value.series
    const fci = charts.get('forecast', fcChart.value)
    fci.setOption({
      grid: { left: 46, right: 16, top: 34, bottom: 26 },
      legend: { textStyle: { color: '#9aa8ba' }, top: 0, itemWidth: 14 },
      tooltip: { trigger: 'axis', ...tooltipStyle },
      xAxis: { type: 'category', data: s.map(x => x.date.slice(5)), ...axisStyle,
        axisLabel: { ...axisStyle.axisLabel, interval: 6 } },
      yAxis: { type: 'value', ...axisStyle, splitLine: { lineStyle: { color: 'rgba(148,163,184,.08)' } } },
      series: [
        { name: '实际负荷', type: 'bar', stack: 'load', barWidth: '62%',
          data: s.map(x => (x.planned ? null : x.load)),
          itemStyle: { color: 'rgba(91,157,255,.75)', borderRadius: [3, 3, 0, 0] } },
        { name: '计划负荷', type: 'bar', stack: 'load', barWidth: '62%',
          data: s.map(x => (x.planned ? x.load : null)),
          itemStyle: { color: 'rgba(255,162,77,.6)', borderRadius: [3, 3, 0, 0] } },
        { name: '体能 CTL', type: 'line', symbol: 'none', smooth: true, data: s.map(x => x.ctl),
          lineStyle: { color: '#c8f169', width: 2 } },
        { name: '疲劳 ATL', type: 'line', symbol: 'none', smooth: true, data: s.map(x => x.atl),
          lineStyle: { color: '#ff6b6b', width: 1.6 } },
        { name: '形态 TSB', type: 'line', symbol: 'none', smooth: true, data: s.map(x => x.tsb),
          lineStyle: { color: '#56d4e0', width: 1.6, type: 'dashed' },
          markLine: fc.value.race ? {
            symbol: 'none', label: { color: '#f5c26b', formatter: '比赛日' },
            lineStyle: { color: 'rgba(245,194,107,.6)', type: 'dotted' },
            data: [{ xAxis: fc.value.race.date.slice(5) }],
          } : undefined },
      ],
    })
  }
}

function rolling(days, n) {
  return days.map((_, i) => {
    const seg = days.slice(Math.max(0, i - n + 1), i + 1)
    return Math.round(seg.reduce((s, x) => s + x.load, 0) / seg.length)
  })
}

onMounted(loadAll)
onUnmounted(charts.disposeAll)
</script>

<style scoped>

.status-card .status-label { font-size: 38px; margin: 6px 0 8px; }
.st-peaking .status-label, .st-productive .status-label { color: var(--lime); }
.st-maintaining .status-label { color: var(--blue); }
.st-unproductive, .st-strained, .st-detached { --status: var(--red); }
.st-unproductive .status-label, .st-strained .status-label, .st-detached .status-label { color: var(--red); }
.st-recovering .status-label { color: var(--cyan); }
.status-detail { color: var(--text-2); font-size: 13px; line-height: 1.7; }
.status-trend { margin-top: 10px; font-size: 12px; color: var(--text-3); }

.ready-wrap { display: flex; justify-content: center; padding: 6px 0; }
.ready-verdict { text-align: center; color: var(--text-2); font-size: 12.5px; margin-top: 4px; }

.rec-row { display: flex; justify-content: space-between; align-items: baseline; padding: 9px 0; border-bottom: 1px dashed rgba(148,163,184,.12); }
.rec-k { color: var(--text-2); font-size: 13px; }
.rec-v { font-size: 30px; color: var(--text); }
.rec-u { font-size: 13px; color: var(--text-3); margin-left: 2px; }
.rec-note { font-size: 11.5px; color: var(--text-3); margin-top: 10px; line-height: 1.6; }

.focus-verdict { font-size: 12.5px; color: var(--text-2); line-height: 1.6; margin: 10px 0; }

.mono-row { display: flex; justify-content: space-around; padding: 4px 0 2px; }
.mono-item { text-align: center; }
.mono-num { font-size: 34px; }
.mono-label { font-size: 11px; color: var(--text-3); margin-top: 4px; }
.mono-verdict { font-size: 12.5px; color: var(--text-2); line-height: 1.65; margin-top: 10px; }
.mono-warn { font-size: 12px; color: var(--orange); line-height: 1.65; margin-top: 6px; }
.focus-bar { display: flex; height: 26px; border-radius: 7px; overflow: hidden; margin-top: 6px; }
.fb-seg { height: 100%; }
.focus-pcts { display: flex; justify-content: space-between; font-size: 12px; font-weight: 700; margin-top: 6px; }
.focus-guide .fg-row { display: flex; align-items: center; font-size: 12px; color: var(--text-2); padding: 3px 0; }
.fg-range { margin-left: auto; color: var(--text-3); font-size: 11px; }

.score-card { text-align: center; }
.score-num { font-size: 46px; }
.score-label { font-size: 14px; font-weight: 700; color: var(--text); margin: 4px 0 8px; }
.score-detail { font-size: 11.5px; color: var(--text-3); text-align: left; line-height: 1.6; }

.comp-row { margin-bottom: 13px; }
.comp-head { display: flex; justify-content: space-between; font-size: 12.5px; color: var(--text-2); margin-bottom: 5px; }
.comp-score { font-family: var(--font-display); font-weight: 700; }
.comp-detail { font-size: 11px; color: var(--text-3); margin-top: 3px; }
</style>
