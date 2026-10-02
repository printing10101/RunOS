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
                  {{ fmtTime(row.interval_sec[0]) }} – {{ fmtTime(row.interval_sec[1]) }}
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
                             :color="row.key === 'marathon' ? '#e05f5f' : '#5f9fc9'" />
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
            <div class="stat-num" style="color:var(--jade)">+{{ ceiling.total_headroom_pct }}%</div>
            <div class="stat-label">预计提升空间 · 约 {{ ceiling.years_to_ceiling }} 年达到</div>
          </div>
          <el-table :data="ceilingRows" size="small" style="margin-top:8px">
            <el-table-column prop="label" width="80" />
            <el-table-column label="当前" width="110">
              <template #default="{ row }">{{ row.current }}</template>
            </el-table-column>
            <el-table-column label="生涯最佳">
              <template #default="{ row }">
                <b style="color:var(--jade)">{{ row.career_best }}</b>
              </template>
            </el-table-column>
          </el-table>
          <el-divider />
          <div style="font-size:13px; font-weight:600; color:var(--text); margin-bottom:8px">提升空间构成</div>
          <div v-for="(v, k) in ceiling.headroom_components" :key="k" class="hc-row">
            <span>{{ k }}</span>
            <el-progress :percentage="parseFloat(v) * 4" :stroke-width="8" :show-text="false" color="#6da8d4" style="flex:1; margin:0 10px" />
            <b style="width:44px; text-align:right">{{ v }}</b>
          </div>
          <el-alert :title="ceiling.disclaimer" type="info" :closable="false" show-icon style="margin-top:12px"
                    v-if="ceiling.disclaimer" />
        </el-card>
      </el-col>
    </el-row>

    <!-- 预测演变：本地引擎逐日快照 × 官方当期预测，看「练得有没有效果」 -->
    <el-card shadow="never" style="margin-top:14px">
      <template #header>
        <div style="display:flex; align-items:center">
          <span>预测演变</span>
          <el-select v-if="trend.races.length > 1" v-model="trendIdx" size="small"
                     style="margin-left:auto; width: 240px">
            <el-option v-for="(r, i) in trend.races" :key="i" :value="i"
                       :label="`${r.race_name || raceTypeName(r.race_type)} · ${r.race_date}`" />
          </el-select>
          <el-tag v-else-if="trendRace" size="small" type="info" style="margin-left:auto">
            {{ trendRace.race_name || raceTypeName(trendRace.race_type) }} · {{ trendRace.race_date }}
          </el-tag>
        </div>
      </template>
      <el-empty v-if="trend.empty"
                description="暂无预测快照：设定带日期的比赛目标后，预测页每次打开都会自动留档当天预测"
                :image-size="70" />
      <template v-else>
        <div ref="trendChart" style="height: 280px"></div>
        <div style="font-size:12px; color:var(--text-3); margin-top:6px">
          曲线走低 = 预测成绩在变好；官方线来自高驰当期评估，仅作能力对照。
          <template v-if="trendRace?.actual">本次已实测 {{ trendRace.actual.time_str }}
            <span :style="{ color: trendRace.actual.delta_pct > 0 ? 'var(--orange)' : 'var(--jade)' }">
              （较赛前预测 {{ trendRace.actual.delta_pct > 0 ? '+' : '' }}{{ trendRace.actual.delta_pct }}%）
            </span>
          </template>
        </div>
      </template>
    </el-card>

    <!-- 预测校准：历次「赛前预测 → 实测」偏差，赛时计划可信度标注同一口径 -->
    <el-card shadow="never" style="margin-top:14px">
      <template #header>
        <div style="display:flex; align-items:center">
          <span>预测校准报告</span>
          <el-tag v-if="calib.n" size="small" type="info" style="margin-left:auto">{{ calib.n }} 场实测 · 平均绝对偏差 {{ calib.mean_abs_pct }}%</el-tag>
        </div>
      </template>
      <el-alert v-if="calib.n" :title="calib.note"
                :type="calib.bias_pct == null ? 'info' : Math.abs(calib.bias_pct) <= 2 ? 'success' : 'warning'"
                :closable="false" show-icon style="margin-bottom:10px" />
      <el-empty v-else :description="calib.note || '还没有实测校准点'" :image-size="60" />
      <el-table v-if="calib.n" :data="calib.items" size="small">
        <el-table-column label="比赛" min-width="140">
          <template #default="{ row }">{{ row.race_name || raceTypeName(row.race_type) }}</template>
        </el-table-column>
        <el-table-column prop="race_date" label="日期" width="110" />
        <el-table-column label="赛前预测" width="100">
          <template #default="{ row }">{{ row.predicted_str }}<span style="color:var(--text-3); font-size:11px">（{{ row.predicted_on }}）</span></template>
        </el-table-column>
        <el-table-column prop="actual_str" label="实测" width="90" />
        <el-table-column label="偏差" width="90">
          <template #default="{ row }">
            <span :style="{ color: row.delta_pct > 2 ? 'var(--orange)' : row.delta_pct < -2 ? 'var(--blue)' : 'var(--jade)' }">
              {{ row.delta_pct > 0 ? '+' : '' }}{{ row.delta_pct }}%
            </span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 减量对照：赛末 3 周计划负荷 vs 推荐衰减带（services/taper.py，只判不调） -->
    <el-card shadow="never" style="margin-top:14px">
      <template #header>
        <div style="display:flex; align-items:center">
          <span>减量对照（Taper）</span>
          <el-tag v-if="!taperPlan.empty && taperPlan.days_to_race != null" size="small"
                  :type="taperPlan.days_to_race <= 14 ? 'danger' : 'info'" style="margin-left:auto">
            距比赛 {{ taperPlan.days_to_race }} 天
          </el-tag>
        </div>
      </template>
      <el-empty v-if="taperPlan.empty" :description="taperPlan.reason || '设定比赛目标并生成计划后，这里对照赛末减量路径'" :image-size="60" />
      <template v-else>
        <el-alert :title="taperPlan.verdict"
                  :type="taperVerdictType" :closable="false" show-icon style="margin-bottom:10px" />
        <el-table :data="taperPlan.weeks" size="small">
          <el-table-column prop="label" label="周" width="90" />
          <el-table-column label="计划负荷" width="100">
            <template #default="{ row }">{{ row.planned_load ?? '-' }}</template>
          </el-table-column>
          <el-table-column label="占峰值周" width="180">
            <template #default="{ row }">
              <template v-if="row.ratio != null">{{ Math.round(row.ratio * 100) }}%
                <span style="color:var(--text-3); font-size:11px">（推荐 {{ Math.round(row.band[0] * 100) }}-{{ Math.round(row.band[1] * 100) }}%）</span>
              </template>
              <span v-else style="color:var(--text-3)">-</span>
            </template>
          </el-table-column>
          <el-table-column label="判定" min-width="240">
            <template #default="{ row }">
              <el-tag size="small" effect="plain" :type="weekTagType(row.verdict)">{{ weekLabel(row) }}</el-tag>
              <span style="font-size:12px; color:var(--text-3); margin-left:6px">{{ row.why }}</span>
            </template>
          </el-table-column>
        </el-table>
        <div style="font-size:12px; color:var(--text-3); margin-top:6px">
          峰值周负荷 {{ taperPlan.peak_week_load }}（全计划按课型估算）；衰减带只是路径参照，调课仍走 AI 提案确认。
        </div>
      </template>
    </el-card>

    <!-- 赛时计划：训练引擎管练什么，这张卡管比赛当天怎么执行（分段/补给/倒计时/预案） -->
    <el-card shadow="never" style="margin-top:14px">
      <template #header>
        <div style="display:flex; align-items:center">
          <span>赛时计划</span>
          <el-tag v-if="!strategy.empty && strategy.race?.days_to_go != null" size="small"
                  style="margin-left:auto" :type="strategy.race.days_to_go <= 14 ? 'danger' : 'info'">
            {{ strategy.race.target_label || strategy.race.label }}
            {{ strategy.race.race_date ? ` · ${strategy.race.race_date}` : '' }}
            {{ strategy.race.days_to_go >= 0 ? ` · 还有 ${strategy.race.days_to_go} 天` : '' }}
          </el-tag>
        </div>
      </template>

      <el-empty v-if="strategy.empty" :description="strategy.reason || '没有进行中的比赛目标'" :image-size="70" />
      <template v-else>
        <el-row :gutter="14">
          <el-col :span="14">
            <div class="pace-hero">
              <div>
                <div class="stat-num">{{ fmtPace(rp.adjusted_pace_sec_per_km) }}</div>
                <div class="stat-label">{{ rp.weather_slowdown_pct ? `目标配速（含高温修正 +${rp.weather_slowdown_pct}%）` : '目标配速（环境适宜，无修正）' }}</div>
              </div>
              <div>
                <div class="stat-num" style="font-size:26px">{{ rp.target_str }}</div>
                <div class="stat-label">目标成绩{{ strategy.race.target_time_sec ? '' : '（取当前预测）' }}</div>
              </div>
              <div v-if="rp.weather_slowdown_pct">
                <div class="stat-num" style="font-size:26px; color:var(--text-2)">{{ fmtPace(rp.pace_sec_per_km) }}</div>
                <div class="stat-label">凉爽天基准</div>
              </div>
            </div>
            <div style="font-size:12.5px; color:var(--text-3); margin:8px 0 10px">{{ rp.strategy_note }}</div>
            <el-alert v-if="strategy.calibration?.note" :title="strategy.calibration.note"
                      :type="strategy.calibration.bias_pct == null ? 'info'
                             : Math.abs(strategy.calibration.bias_pct) <= 2 ? 'success' : 'warning'"
                      :closable="false" show-icon style="margin-bottom:10px" />
            <el-table :data="rp.splits" size="small">
              <el-table-column label="分段" width="90">
                <template #default="{ row }">{{ row.km }} km</template>
              </el-table-column>
              <el-table-column label="段配速" width="110">
                <template #default="{ row }">{{ fmtPace(row.lap_sec) }}</template>
              </el-table-column>
              <el-table-column label="累计用时">
                <template #default="{ row }">{{ row.cumulative_str }}</template>
              </el-table-column>
            </el-table>
            <div v-if="strategy.prediction?.delta_vs_target_sec != null" style="margin-top:8px; font-size:12.5px"
                 :style="{ color: strategy.prediction.delta_vs_target_sec > 0 ? 'var(--orange)' : 'var(--jade)' }">
              当前预测 {{ strategy.prediction.predicted_str }}，较目标{{
                strategy.prediction.delta_vs_target_sec > 0
                  ? `还差 ${fmtTime(Math.abs(strategy.prediction.delta_vs_target_sec))}`
                  : `已超出 ${fmtTime(Math.abs(strategy.prediction.delta_vs_target_sec))}` }}
            </div>
          </el-col>
          <el-col :span="10">
            <div class="sub-head">补给时间表</div>
            <div style="font-size:13px; line-height:1.8; color:var(--text-2)">
              {{ strategy.fueling.note }}
              <div v-if="strategy.fueling.needed" style="margin-top:4px">
                <el-tag v-for="(t, i) in strategy.fueling.schedule" :key="i" size="small" effect="plain"
                        style="margin:2px 4px 0 0">🧴 {{ t }}</el-tag>
              </div>
            </div>
            <el-divider />
            <div class="sub-head">赛前倒计时</div>
            <div v-for="c in strategy.countdown" :key="c.day_offset" class="cd-row">
              <el-tag size="small" effect="dark" :type="c.day_offset === 0 ? 'danger' : 'info'" style="flex-shrink:0">
                {{ c.day_offset === 0 ? '比赛日' : `D${c.day_offset}` }}
              </el-tag>
              <span>{{ c.item }}</span>
            </div>
            <el-divider />
            <div class="sub-head">环境预案</div>
            <div v-for="c in strategy.contingencies" :key="c.condition" class="cd-row">
              <el-tag size="small" :type="c.applies ? 'warning' : 'info'" effect="plain" style="flex-shrink:0">
                {{ c.condition }}
              </el-tag>
              <span>{{ c.advice }}</span>
            </div>
          </el-col>
        </el-row>
      </template>
    </el-card>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, fmtPace, fmtTime } from '../api'
import { axisStyle, tooltipStyle, splitLineStyle } from '../utils/echarts-theme'
import { raceTypeName, todayStr } from '../utils/common'
import { createChartManager } from '../composables/charts'

const a = ref({})
const ceilingChart = ref(null)
const loading = ref(true)
const charts = createChartManager()
// 赛时计划：独立拉取，失败不拦预测主卡（空态由卡片自己呈现）
const strategy = ref({ empty: true })
const rp = computed(() => strategy.value.pace_plan || {})
// 预测演变：独立拉取，失败不拦主卡
const trend = ref({ empty: true, races: [] })
const trendIdx = ref(0)
const trendChart = ref(null)
const trendRace = computed(() => trend.value.races[trendIdx.value] || null)
// 预测校准报告：与赛时计划的可信度标注同一取数
const calib = ref({ empty: true, n: 0, items: [], note: '' })
// 减量对照（taper）：独立拉取，失败不拦主卡
const taperPlan = ref({ empty: true })

const taperVerdictType = computed(() => {
  const v = taperPlan.value.verdict || ''
  return v.includes('不足') ? 'warning' : v.includes('偏大') ? 'info' : v.includes('符合') ? 'success' : 'info'
})
const weekTagType = v => v === 'ok' ? 'success' : v === 'high' ? 'warning' : v === 'low' ? 'info' : 'info'
// no_plan 后端一词两义（该周未覆盖 / 峰值负荷过小都判 no_plan，见 taper.py），
// 标签按 why 区分开，避免「计划未覆盖」与旁边 why 文案自相矛盾
const weekLabel = row => {
  if (row.verdict !== 'no_plan') {
    return { ok: '达标', high: '减量不足', low: '减量偏大' }[row.verdict] || row.verdict
  }
  return (row.why || '').includes('峰值') ? '峰值过小' : '计划未覆盖'
}

const predRows = computed(() => {
  const preds = a.value.predictions || {}
  const rows = Object.entries(preds).map(([k, p]) => ({ key: k, label: raceTypeName(k), ...p }))
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

const ceiling = computed(() => a.value.career_ceiling || {})
const ceilingRows = computed(() =>
  Object.entries(ceiling.value.ceiling || {}).map(([k, v]) => ({ key: k, label: raceTypeName(k), ...v })))

// 目标成绩串（"h:mm:ss"）→ 分钟数，画柱状图用
function strToMin(str) {
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
    legend: { top: 0, textStyle: { color: '#8fada2' } },
    xAxis: { type: 'category', data: rows.map(r => r.label), ...axisStyle },
    yAxis: { type: 'value', name: '分钟', nameTextStyle: { color: '#5c6f68' }, ...axisStyle, splitLine: splitLineStyle },
    series: [
      { name: '当前预测', type: 'bar', barWidth: 22, itemStyle: { color: 'rgba(95,159,201,0.55)', borderRadius: [6, 6, 0, 0] },
        data: rows.map(r => +strToMin(r.current).toFixed(1)) },
      { name: '生涯上限', type: 'bar', barWidth: 22, itemStyle: { color: '#3fd0a4', borderRadius: [6, 6, 0, 0] },
        data: rows.map(r => +(r.career_best_sec / 60).toFixed(1)) },
    ],
  })
}

onMounted(async () => {
  try {
    a.value = await api.post('/assessment/compute', {})
    render()
  } finally { loading.value = false }
  api.get('/races/strategy').then(r => { strategy.value = r }).catch(() => {})
  api.get('/stats/prediction-trend').then(r => {
    trend.value = r
    if (r.races.length) {
      // 默认选最近一场还没跑的比赛；都跑完了就选最近一场
      const today = todayStr()
      const idx = r.races.findIndex(x => x.race_date >= today)
      trendIdx.value = idx >= 0 ? idx : r.races.length - 1
      nextTick(renderTrend)
    }
  }).catch(() => {})
  api.get('/stats/prediction-calibration').then(r => { calib.value = r }).catch(() => {})
  api.get('/races/taper-check').then(r => { taperPlan.value = r }).catch(() => {})
})

watch(trendIdx, renderTrend)

function renderTrend() {
  const g = trendRace.value
  // onMounted 改选默认场次会先于 DOM 更新触发 watcher：容器还没挂载时直接跳过，
  // 等 nextTick 的那次调用再画（否则 echarts.init(null) 抛错弹全局错误提示）
  if (!trendChart.value || !g || !g.local_series?.length) return
  const secToMin = sec => +(sec / 60).toFixed(2)
  // 两条曲线的快照日集合可能不同：x 轴取并集，各自缺的日子留 null 断点
  const dates = [...new Set([...g.local_series, ...(g.official_series || [])].map(p => p.date))].sort()
  const pick = series => dates.map(d => {
    const p = series.find(x => x.date === d)
    return p ? secToMin(p.predicted_sec) : null
  })
  charts.get('trend', trendChart.value).setOption({
    grid: { left: 52, right: 20, top: 34, bottom: 26 },
    legend: { top: 0, textStyle: { color: '#8fada2' } },
    tooltip: { trigger: 'axis', ...tooltipStyle },
    xAxis: { type: 'category', data: dates.map(d => d.slice(5)), ...axisStyle },
    yAxis: { type: 'value', name: '分钟', nameTextStyle: { color: '#5c6f68' }, ...axisStyle, splitLine: splitLineStyle },
    series: [
      { name: '本地预测', type: 'line', smooth: true, symbolSize: 5, connectNulls: true,
        data: pick(g.local_series),
        lineStyle: { color: '#3fd0a4', width: 2 },
        itemStyle: { color: '#3fd0a4' } },
      ...(g.official_series?.length ? [{
        name: '官方预测（高驰）', type: 'line', smooth: true, symbolSize: 4, connectNulls: true,
        data: pick(g.official_series),
        lineStyle: { color: '#4fc3c7', width: 1.6, type: 'dashed' },
        itemStyle: { color: '#4fc3c7' } }] : []),
    ],
  })
}

onUnmounted(charts.disposeAll)
</script>

<style scoped>
.ceiling-hero { text-align: center; padding: 6px 0 2px; }
.hc-row { display: flex; align-items: center; font-size: 13px; color: var(--text-2); padding: 5px 0; }
.pace-hero { display: flex; gap: 40px; align-items: baseline; }
.sub-head { font-size: 13px; font-weight: 600; color: var(--text); margin-bottom: 6px; }
.cd-row { display: flex; gap: 8px; align-items: flex-start; font-size: 12.5px; color: var(--text-2); line-height: 1.7; margin: 4px 0; }
</style>
