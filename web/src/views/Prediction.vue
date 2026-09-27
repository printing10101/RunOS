<template>
  <div v-loading="loading">
    <el-row :gutter="14">
      <el-col :span="14">
        <el-card shadow="never">
          <template #header>
            <div style="display:flex; align-items:center">
              <span>当前成绩预测</span>
              <el-tag size="small" style="margin-left:auto" type="info">数据质量：{{ a.data_quality || '-' }}</el-tag>
            </div>
          </template>
          <el-table :data="predRows" size="large">
            <el-table-column prop="label" width="110" />
            <el-table-column label="预测成绩" width="120">
              <template #default="{ row }">
                <b style="font-size:16px; color:var(--text)">{{ row.time_str }}</b>
              </template>
            </el-table-column>
            <el-table-column label="模型区间" width="150">
              <template #default="{ row }">
                <span v-if="row.interval_sec" style="color:var(--text-2); font-size:13px">
                  {{ fmtSec(row.interval_sec[0]) }} – {{ fmtSec(row.interval_sec[1]) }}
                </span>
                <span v-else style="color:var(--text-3)">-</span>
              </template>
            </el-table-column>
            <el-table-column label="配速" width="110">
              <template #default="{ row }">{{ row.pace }}</template>
            </el-table-column>
            <el-table-column label="对应 VDOT" width="110">
              <template #default="{ row }">{{ row.vdot }}</template>
            </el-table-column>
            <el-table-column label="当前水平" min-width="160">
              <template #default="{ row }">
                <el-progress :percentage="row.bar" :stroke-width="8" :show-text="false"
                             :color="row.key === 'marathon' ? '#ff6b6b' : '#5b9dff'" />
              </template>
            </el-table-column>
          </el-table>
          <div style="margin-top:10px; font-size:12px; color:var(--text-3)">
            当前 VDOT {{ a.current_vdot }} · 临界速度 {{ a.critical_speed ?? '-' }} m/s
            <template v-if="a.quality?.cs_r2 != null">（拟合优度 R²={{ a.quality.cs_r2 }}）</template> ·
            {{ qualityLine }} ·
            预测为模型估计，建议每 8-12 周通过一场测试赛校准
          </div>
        </el-card>

        <el-card shadow="never" style="margin-top:14px">
          <template #header>当前 vs 生涯上限</template>
          <div ref="ceilingChart" style="height: 280px"></div>
        </el-card>
      </el-col>

      <el-col :span="10">
        <el-card shadow="never">
          <template #header>
            <div style="display:flex; align-items:center">
              <span>生涯可能达到的最好成绩</span>
            </div>
          </template>
          <div class="ceiling-hero" v-if="ceiling.total_headroom_pct != null">
            <div class="stat-num" style="color:var(--lime)">+{{ ceiling.total_headroom_pct }}%</div>
            <div class="stat-label">预计提升空间 · 约 {{ ceiling.years_to_ceiling }} 年达到</div>
          </div>
          <el-table :data="ceilingRows" size="small" style="margin-top:8px">
            <el-table-column prop="label" width="80" />
            <el-table-column label="当前" width="110">
              <template #default="{ row }">{{ row.current }}</template>
            </el-table-column>
            <el-table-column label="生涯最佳">
              <template #default="{ row }">
                <b style="color:var(--lime)">{{ row.career_best }}</b>
              </template>
            </el-table-column>
          </el-table>
          <el-divider />
          <div style="font-size:13px; font-weight:600; color:var(--text); margin-bottom:8px">提升空间构成</div>
          <div v-for="(v, k) in ceiling.headroom_components" :key="k" class="hc-row">
            <span>{{ k }}</span>
            <el-progress :percentage="parseFloat(v) * 4" :stroke-width="8" :show-text="false" color="#60a5fa" style="flex:1; margin:0 10px" />
            <b style="width:44px; text-align:right">{{ v }}</b>
          </div>
          <el-alert :title="ceiling.disclaimer" type="info" :closable="false" show-icon style="margin-top:12px"
                    v-if="ceiling.disclaimer" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'
import { axisStyle, tooltipStyle, splitLineStyle } from '../utils/echarts-theme'
import { createChartManager } from '../composables/charts'

const a = ref({})
const ceilingChart = ref(null)
const loading = ref(true)
const charts = createChartManager()

const LABELS = { '800m': '800米', '1500m': '1500米', '3k': '3公里', '5k': '5公里', '10k': '10公里', hm: '半马', marathon: '全马' }

const predRows = computed(() => {
  const preds = a.value.predictions || {}
  const rows = Object.entries(preds).map(([k, p]) => ({ key: k, label: LABELS[k] || k, ...p }))
  const maxSec = Math.max(...rows.map(r => r.time_sec), 1)
  rows.forEach(r => { r.bar = Math.round((1 - r.time_sec / maxSec / 1.05) * 100) })
  return rows
})

// 结构化数据质量行：成绩样本数、Riegel 校准配对数、CS 拟合可信度
const qualityLine = computed(() => {
  const q = a.value.quality || {}
  const parts = [`样本 ${q.efforts ?? '-'} 场`]
  parts.push(q.riegel_pairs ? `校准 ${q.riegel_pairs} 对` : '未校准')
  if (q.cs_r2 != null) parts.push(q.cs_reliable ? 'CS 拟合可靠' : 'CS 拟合一致性欠佳')
  return parts.join(' · ')
})

// 秒 → h:mm:ss / mm:ss（区间端点展示）
function fmtSec(sec) {
  if (sec == null) return '-'
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = Math.round(sec % 60)
  const mm = String(m).padStart(2, '0')
  const ss = String(s).padStart(2, '0')
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`
}

const ceiling = computed(() => a.value.career_ceiling || {})
const ceilingRows = computed(() =>
  Object.entries(ceiling.value.ceiling || {}).map(([k, v]) => ({ key: k, label: LABELS[k] || k, ...v })))

function toMin(str) {
  if (!str) return 0
  const parts = str.split(':').map(Number)
  return parts.length === 3 ? parts[0] * 60 + parts[1] + parts[2] / 60 : parts[0] + parts[1] / 60
}

function render() {
  const rows = ceilingRows.value.filter(r => r.career_best_sec)
  if (!rows.length) return
  charts.get('ceiling', ceilingChart.value).setOption({
    grid: { left: 44, right: 16, top: 30, bottom: 26 },
    tooltip: { trigger: 'axis', ...tooltipStyle },
    legend: { top: 0, textStyle: { color: '#9aa8ba' } },
    xAxis: { type: 'category', data: rows.map(r => r.label), ...axisStyle },
    yAxis: { type: 'value', name: '分钟', nameTextStyle: { color: '#5f6d80' }, ...axisStyle, splitLine: splitLineStyle },
    series: [
      { name: '当前预测', type: 'bar', barWidth: 22, itemStyle: { color: 'rgba(91,157,255,0.55)', borderRadius: [6, 6, 0, 0] },
        data: rows.map(r => +toMin(r.current).toFixed(1)) },
      { name: '生涯上限', type: 'bar', barWidth: 22, itemStyle: { color: '#c8f169', borderRadius: [6, 6, 0, 0] },
        data: rows.map(r => +(r.career_best_sec / 60).toFixed(1)) },
    ],
  })
}

onMounted(async () => {
  try {
    a.value = await api.post('/assessment/compute', {})
    render()
  } finally { loading.value = false }
})

onUnmounted(charts.disposeAll)
</script>

<style scoped>
.ceiling-hero { text-align: center; padding: 6px 0 2px; }
.hc-row { display: flex; align-items: center; font-size: 13px; color: var(--text-2); padding: 5px 0; }
</style>
