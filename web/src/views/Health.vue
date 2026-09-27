<template>
  <el-skeleton v-if="loading" style="margin-top:8px" :rows="8" animated />
  <el-empty v-else-if="loadError" :description="loadError">
    <el-button type="primary" round @click="loadAll">重试</el-button>
  </el-empty>
  <div v-else-if="!d.empty">
    <div style="display:flex; justify-content:flex-end; margin-bottom:10px">
      <el-button type="primary" round @click="openEntry">＋ 录入 / 修正</el-button>
    </div>

    <template v-if="hasData">
      <!-- 佳明式状态速览：按 7 日均值对比基线判定，比下方「最新值」更能反映近期恢复趋势 -->
      <div class="ov-strip" v-if="overview?.has_data">
        <div class="ov-item">
          <span class="ov-k">HRV 状态（7 日）</span>
          <span class="ov-v" :style="{ color: ovHrvColor }">{{ overview.hrv?.status || '数据不足' }}</span>
          <span class="ov-s">7 日均 {{ overview.hrv?.last7_avg ?? '-' }} · 基线 {{ overview.hrv?.baseline ?? '-' }} ms</span>
          <span class="ov-s" v-if="overview.baselines?.hrv_rmssd?.baseline != null">
            数据基线 {{ overview.baselines.hrv_rmssd.baseline }} · {{ overview.baselines.hrv_rmssd.status }}
          </span>
        </div>
        <div class="ov-div"></div>
        <div class="ov-item">
          <span class="ov-k">睡眠（7 日均）</span>
          <span class="ov-v">{{ overview.sleep?.last7_hours ?? '-' }}<span class="ov-u">h</span></span>
          <span class="ov-s" v-if="overview.sleep?.last7_score != null">睡眠分 {{ overview.sleep.last7_score }}</span>
        </div>
        <div class="ov-div"></div>
        <div class="ov-item">
          <span class="ov-k">静息心率（7 日均）</span>
          <span class="ov-v">{{ overview.resting_hr?.last7_avg ?? '-' }}<span class="ov-u">bpm</span></span>
          <span class="ov-s">基线 {{ overview.baseline?.resting_hr ?? '-' }}</span>
        </div>
        <div class="ov-div"></div>
        <div class="ov-item">
          <span class="ov-k">已记录</span>
          <span class="ov-v">{{ overview.days_recorded ?? '-' }}<span class="ov-u">天</span></span>
        </div>
      </div>
      <div class="metric-grid">
        <div class="metric">
          <div class="num-display m-val" :style="{ color: hrvColor }">{{ latest('hrv_rmssd') ?? '-' }}</div>
          <div class="m-label">HRV rmssd (ms)</div>
          <div class="m-note">{{ hrvStatusText }}</div>
        </div>
        <div class="metric">
          <div class="num-display m-val">{{ latest('sleep_hours') ?? '-' }}<span class="m-sub">h</span>
            <span v-if="latest('sleep_score') != null" class="m-sub">· {{ latest('sleep_score') }}分</span></div>
          <div class="m-label">睡眠</div>
          <div class="m-note">{{ latest('sleep_hours') >= 7 ? '充足' : '不足 7h' }}</div>
        </div>
        <div class="metric">
          <div class="num-display m-val">{{ latest('resting_hr') ?? '-' }}</div>
          <div class="m-label">静息心率 bpm</div>
          <div class="m-note">基线 {{ d.baseline?.resting_hr ?? '-' }}</div>
        </div>
        <div class="metric">
          <div class="num-display m-val">{{ latest('weight_kg') ?? '-' }}</div>
          <div class="m-label">体重 kg<template v-if="latest('body_fat_pct') != null"> · 体脂 {{ latest('body_fat_pct') }}%</template></div>
          <div class="m-note">档案目标 {{ d.baseline?.weight_kg }}kg</div>
        </div>
        <div class="metric" v-if="latest('spo2') != null">
          <div class="num-display m-val">{{ latest('spo2') }}<span class="m-sub">%</span></div>
          <div class="m-label">血氧 SpO₂</div>
          <div class="m-note">正常 ≥95%</div>
        </div>
        <div class="metric" v-if="latest('resp_rate') != null">
          <div class="num-display m-val">{{ latest('resp_rate') }}</div>
          <div class="m-label">呼吸率 /分</div>
        </div>
        <div class="metric" v-if="latest('stress') != null">
          <div class="num-display m-val" :style="{ color: stressColor }">{{ latest('stress') }}</div>
          <div class="m-label">压力分数</div>
        </div>
        <div class="metric" v-if="latest('body_battery') != null">
          <div class="num-display m-val" style="color: var(--jade)">{{ latest('body_battery') }}</div>
          <div class="m-label">身体电量</div>
        </div>
      </div>

      <el-row :gutter="14" style="margin-top:14px">
        <el-col :span="12">
          <el-card shadow="never">
            <template #header><div class="card-head">HRV 趋势 <span class="card-sub">虚线为基线 {{ d.baseline?.hrv_baseline ?? '-' }} ms</span></div></template>
            <div v-if="chartHas.hrv" ref="hrvChart" style="height: 240px"></div>
            <el-empty v-else description="暂无 HRV 数据，可在右上角手动录入，或接入佳明同步" :image-size="60" />
          </el-card>
        </el-col>
        <el-col :span="12">
          <el-card shadow="never">
            <template #header><div class="card-head">睡眠 <span class="card-sub">时长 + 分数</span></div></template>
            <div v-if="chartHas.sleep" ref="sleepChart" style="height: 240px"></div>
            <el-empty v-else description="暂无睡眠数据" :image-size="60" />
          </el-card>
        </el-col>
      </el-row>
      <el-row :gutter="14" style="margin-top:14px">
        <el-col :span="12">
          <el-card shadow="never">
            <template #header><div class="card-head">体重 / 体脂 <span class="card-sub">近 {{ rangeDays }} 天</span></div></template>
            <div v-if="chartHas.weight" ref="weightChart" style="height: 240px"></div>
            <el-empty v-else description="暂无体重 / 体脂记录，手动录入后即可跟踪趋势" :image-size="60" />
          </el-card>
        </el-col>
        <el-col :span="12">
          <el-card shadow="never">
            <template #header><div class="card-head">静息心率趋势</div></template>
            <div v-if="chartHas.rhr" ref="rhrChart" style="height: 240px"></div>
            <el-empty v-else description="暂无静息心率数据" :image-size="60" />
          </el-card>
        </el-col>
      </el-row>
    </template>
    <el-empty v-else description="还没有身体数据：接入佳明同步，或点右上角手动录入（HRV/睡眠/体重…）">
      <el-button type="primary" round @click="openEntry">手动录入第一条</el-button>
    </el-empty>

    <!-- 晨间打卡趋势：主观状态不再只是「当天一次性」的问卷，攒出来的数据在这里回头看 -->
    <el-card v-if="checkinItems.length" shadow="never" style="margin-top:14px">
      <template #header>
        <div class="card-head">晨间打卡趋势
          <span class="card-sub">近 30 天 · 已连续打卡 {{ checkinStreak }} 天 · 酸痛越低越好</span>
        </div>
      </template>
      <div v-if="checkinItems.length >= 2" ref="checkinChart" style="height: 220px"></div>
      <el-empty v-else description="打卡满 2 天后这里会出现趋势曲线" :image-size="60" />
    </el-card>

    <!-- 录入弹窗 -->
    <el-dialog v-model="entryVisible" title="录入 / 修正身体数据（按日期覆盖）" width="520px">
      <el-form :model="form" label-width="110px" size="default">
        <el-form-item label="日期"><el-date-picker v-model="form.date" type="date" value-format="YYYY-MM-DD" style="width:100%" /></el-form-item>
        <div class="form-grid">
          <el-form-item label="HRV (ms)"><el-input-number v-model="form.hrv_rmssd" :min="10" :max="160" :precision="1" controls-position="right" /></el-form-item>
          <el-form-item label="睡眠 (h)"><el-input-number v-model="form.sleep_hours" :min="0" :max="14" :precision="1" controls-position="right" /></el-form-item>
          <el-form-item label="睡眠分数"><el-input-number v-model="form.sleep_score" :min="0" :max="100" controls-position="right" /></el-form-item>
          <el-form-item label="静息心率"><el-input-number v-model="form.resting_hr" :min="35" :max="100" controls-position="right" /></el-form-item>
          <el-form-item label="体重 (kg)"><el-input-number v-model="form.weight_kg" :min="30" :max="150" :precision="1" controls-position="right" /></el-form-item>
          <el-form-item label="体脂 (%)"><el-input-number v-model="form.body_fat_pct" :min="3" :max="60" :precision="1" controls-position="right" /></el-form-item>
          <el-form-item label="血氧 (%)"><el-input-number v-model="form.spo2" :min="70" :max="100" :precision="1" controls-position="right" /></el-form-item>
          <el-form-item label="呼吸率"><el-input-number v-model="form.resp_rate" :min="6" :max="30" :precision="1" controls-position="right" /></el-form-item>
          <el-form-item label="压力分数"><el-input-number v-model="form.stress" :min="0" :max="100" controls-position="right" /></el-form-item>
          <el-form-item label="身体电量"><el-input-number v-model="form.body_battery" :min="0" :max="100" controls-position="right" /></el-form-item>
        </div>
      </el-form>
      <template #footer>
        <el-button round @click="entryVisible = false">取消</el-button>
        <el-button type="primary" round @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
  <el-empty v-else description="请先完善个人档案" />
</template>

<script setup>
import { todayStr, localDateStr } from '../utils/common'
import { computed, onMounted, onUnmounted, ref, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { axisStyle, tooltipStyle } from '../utils/echarts-theme'
import { createChartManager } from '../composables/charts'

const d = ref({ empty: true, metrics: [] })
const overview = ref(null)          // /health/overview 佳明式状态速览（辅助视图，失败置空不拦页面）
const checkinItems = ref([])        // /checkin/history 近 30 天晨间打卡
const rangeDays = 90
const entryVisible = ref(false)
const form = ref({})
const loadError = ref('')
const loading = ref(true)
const hrvChart = ref(null)
const sleepChart = ref(null)
const weightChart = ref(null)
const rhrChart = ref(null)
const checkinChart = ref(null)
// 统一管理图表实例：init 去重、窗口缩放自动 resize、卸载时释放
const charts = createChartManager()

const metrics = computed(() => d.value.metrics || [])
const withData = computed(() => metrics.value.filter(m => Object.keys(m).some(k => k !== 'date' && m[k] != null)))
const hasData = computed(() => withData.value.length > 0)
// 各图表是否有可绘数据：无数据时显示引导提示，而不是渲染一张空白图
const chartHas = computed(() => {
  const ms = withData.value.slice(-rangeDays)
  const has = (k) => ms.some(m => m[k] != null)
  return {
    hrv: has('hrv_rmssd'),
    sleep: has('sleep_hours') || has('sleep_score'),
    weight: has('weight_kg') || has('body_fat_pct'),
    rhr: has('resting_hr'),
  }
})
const latest = (key) => {
  for (let i = metrics.value.length - 1; i >= 0; i--) {
    if (metrics.value[i][key] != null) return metrics.value[i][key]
  }
  return null
}
const hrvStatusText = computed(() => {
  const base = d.value.baseline?.hrv_baseline
  const cur = latest('hrv_rmssd')
  if (!base || cur == null) return `基线 ${base ?? '-'} ms`
  const ratio = cur / base
  return ratio >= 0.95 ? `高于基线 ${((ratio - 1) * 100).toFixed(0)}% · 恢复良好` :
         ratio >= 0.85 ? '接近基线' : '明显低于基线 · 注意恢复'
})
const hrvColor = computed(() => {
  const base = d.value.baseline?.hrv_baseline
  const cur = latest('hrv_rmssd')
  if (!base || cur == null) return '#dce8e2'
  return cur / base >= 0.95 ? '#5fc987' : cur / base >= 0.85 ? '#d9a24e' : '#e05f5f'
})
const stressColor = computed(() => {
  const s = latest('stress')
  if (s == null) return '#dce8e2'
  return s <= 40 ? '#5fc987' : s <= 60 ? '#d9a24e' : '#e05f5f'
})

const OV_STATUS_COLOR = { '平衡': '#5fc987', '偏低': '#d9a24e', '不平衡': '#e05f5f' }
const ovHrvColor = computed(() => OV_STATUS_COLOR[overview.value?.hrv?.status] || '#dce8e2')

// 连续打卡天数：从今天往回数；今天还没打不算断档（从昨天起算）
const checkinStreak = computed(() => {
  const dates = new Set(checkinItems.value.map(i => i.date))
  const cur = new Date()
  if (!dates.has(todayStr())) cur.setDate(cur.getDate() - 1)
  let n = 0
  while (dates.has(localDateStr(cur))) { n += 1; cur.setDate(cur.getDate() - 1) }
  return n
})

const grid = { left: 42, right: 16, top: 28, bottom: 26 }

function renderCharts() {
  const ms = withData.value.slice(-rangeDays)
  if (!ms.length) return
  const dates = ms.map(m => String(m.date).slice(5))
  const base = d.value.baseline?.hrv_baseline

  if (hrvChart.value) {
    const vals = ms.map(m => m.hrv_rmssd)
    if (vals.some(v => v != null)) {
      // 客观数据基线（28 天中位数 ± MAD 波动带）来自 /health/overview 的 baselines 块；
      // 档案基线虚线保留——一条是用户声明口径，一条是引擎估计
      const bl = overview.value?.baselines?.hrv_rmssd || {}
      const bandBase = bl.baseline
      const bandUp = bandBase != null && bl.spread != null ? bandBase + 2 * bl.spread : null
      const bandLo = bandBase != null && bl.spread != null ? bandBase - 2 * bl.spread : null
      charts.get('hrv', hrvChart.value).setOption({
        grid, tooltip: { trigger: 'axis', ...tooltipStyle },
        legend: { textStyle: { color: '#8fada2' }, top: 0, data: ['HRV', '档案基线'] },
        xAxis: { type: 'category', data: dates, ...axisStyle, axisLabel: { ...axisStyle.axisLabel, interval: 12 } },
        yAxis: { type: 'value', ...axisStyle, scale: true, splitLine: { lineStyle: { color: 'rgba(157,184,173,.08)' } } },
        series: [
          ...(bandUp != null ? [
            { name: '基线上界', type: 'line', data: dates.map(() => bandUp), symbol: 'none',
              lineStyle: { color: 'rgba(92,111,104,0.5)', type: 'dotted', width: 1 }, stack: 'band', silent: true },
            { name: '基线带', type: 'line', data: dates.map(() => bandLo - bandUp), symbol: 'none',
              lineStyle: { opacity: 0 }, stack: 'band', areaStyle: { color: 'rgba(92,111,104,0.10)' }, silent: true },
          ] : []),
          ...(base ? [{ name: '档案基线', type: 'line', data: dates.map(() => base), symbol: 'none',
            lineStyle: { color: '#5c6f68', type: 'dashed', width: 1.2 } }] : []),
          { name: 'HRV', type: 'line', data: vals, smooth: true, symbol: 'circle', symbolSize: 3,
            lineStyle: { color: '#4fc3c7', width: 2 },
            areaStyle: { color: 'rgba(86,212,224,0.08)' } },
        ],
      })
    }
  }
  if (sleepChart.value) {
    const hours = ms.map(m => m.sleep_hours)
    const scores = ms.map(m => m.sleep_score)
    if (hours.some(v => v != null)) {
      charts.get('sleep', sleepChart.value).setOption({
        grid, tooltip: { trigger: 'axis', ...tooltipStyle },
        legend: { textStyle: { color: '#8fada2' }, top: 0 },
        xAxis: { type: 'category', data: dates, ...axisStyle, axisLabel: { ...axisStyle.axisLabel, interval: 12 } },
        yAxis: [
          { type: 'value', ...axisStyle, max: v => Math.ceil(v.max + 1), splitLine: { lineStyle: { color: 'rgba(157,184,173,.08)' } } },
          { type: 'value', ...axisStyle, min: 0, max: 100, splitLine: { show: false } },
        ],
        series: [
          { name: '时长(h)', type: 'bar', data: hours.map(v => ({ value: v,
              itemStyle: { color: v >= 7 ? 'rgba(95, 201, 135,.75)' : 'rgba(217,162,78,.8)', borderRadius: [3, 3, 0, 0] } })),
            barWidth: '60%' },
          ...(scores.some(v => v != null) ? [{ name: '分数', type: 'line', yAxisIndex: 1, data: scores,
            symbol: 'none', smooth: true, lineStyle: { color: '#5f9fc9', width: 1.8 } }] : []),
        ],
      })
    }
  }
  if (weightChart.value) {
    const w = ms.map(m => m.weight_kg), f = ms.map(m => m.body_fat_pct)
    if (w.some(v => v != null)) {
      charts.get('weight', weightChart.value).setOption({
        grid, tooltip: { trigger: 'axis', ...tooltipStyle },
        legend: { textStyle: { color: '#8fada2' }, top: 0 },
        xAxis: { type: 'category', data: dates, ...axisStyle, axisLabel: { ...axisStyle.axisLabel, interval: 12 } },
        yAxis: [
          { type: 'value', ...axisStyle, scale: true, splitLine: { lineStyle: { color: 'rgba(157,184,173,.08)' } } },
          { type: 'value', ...axisStyle, scale: true, splitLine: { show: false } },
        ],
        series: [
          { name: '体重 kg', type: 'line', data: w, smooth: true, symbol: 'circle', symbolSize: 3,
            lineStyle: { color: '#3fd0a4', width: 2 } },
          ...(f.some(v => v != null) ? [{ name: '体脂 %', type: 'line', yAxisIndex: 1, data: f,
            symbol: 'none', smooth: true, lineStyle: { color: '#d9a24e', width: 1.6, type: 'dashed' } }] : []),
        ],
      })
    }
  }
  if (rhrChart.value) {
    const vals = ms.map(m => m.resting_hr)
    if (vals.some(v => v != null)) {
      charts.get('rhr', rhrChart.value).setOption({
        grid, tooltip: { trigger: 'axis', ...tooltipStyle },
        xAxis: { type: 'category', data: dates, ...axisStyle, axisLabel: { ...axisStyle.axisLabel, interval: 12 } },
        yAxis: { type: 'value', ...axisStyle, scale: true, splitLine: { lineStyle: { color: 'rgba(157,184,173,.08)' } } },
        series: [{ name: '静息心率', type: 'line', data: vals, smooth: true, symbol: 'circle', symbolSize: 3,
          lineStyle: { color: '#e05f5f', width: 2 },
          areaStyle: { color: 'rgba(224,95,95,0.06)' } }],
      })
    }
  }
}

function renderCheckinChart() {
  const items = checkinItems.value
  if (items.length < 2 || !checkinChart.value) return
  charts.get('checkin', checkinChart.value).setOption({
    grid: { left: 34, right: 16, top: 30, bottom: 26 },
    tooltip: { trigger: 'axis', ...tooltipStyle },
    legend: { textStyle: { color: '#8fada2' }, top: 0 },
    xAxis: { type: 'category', data: items.map(i => String(i.date).slice(5)),
             ...axisStyle, axisLabel: { ...axisStyle.axisLabel, interval: 3 } },
    yAxis: { type: 'value', min: 0, max: 5, interval: 1, ...axisStyle,
             splitLine: { lineStyle: { color: 'rgba(157,184,173,.08)' } } },
    series: [
      { name: '睡眠质量', type: 'line', data: items.map(i => i.sleep_quality), smooth: true,
        symbol: 'circle', symbolSize: 4, lineStyle: { color: '#5f9fc9', width: 2 }, itemStyle: { color: '#5f9fc9' } },
      { name: '精力', type: 'line', data: items.map(i => i.energy_level), smooth: true,
        symbol: 'circle', symbolSize: 4, lineStyle: { color: '#5fc987', width: 2 }, itemStyle: { color: '#5fc987' } },
      { name: '动力', type: 'line', data: items.map(i => i.motivation), smooth: true,
        symbol: 'circle', symbolSize: 4, lineStyle: { color: '#3fd0a4', width: 2 }, itemStyle: { color: '#3fd0a4' } },
      { name: '酸痛', type: 'line', data: items.map(i => i.muscle_soreness), smooth: true,
        symbol: 'circle', symbolSize: 4, lineStyle: { color: '#e05f5f', width: 1.6, type: 'dashed' },
        itemStyle: { color: '#e05f5f' } },
    ],
  })
}

function openEntry() {
  form.value = { date: todayStr() }
  entryVisible.value = true
}

const saving = ref(false)
async function save() {
  if (!form.value.date) { ElMessage.warning('请选择日期'); return }
  if (saving.value) return
  saving.value = true
  try {
    const body = { date: form.value.date }
    for (const k of ['hrv_rmssd', 'sleep_hours', 'sleep_score', 'resting_hr', 'weight_kg',
                     'body_fat_pct', 'spo2', 'resp_rate', 'stress', 'body_battery']) {
      if (form.value[k] != null) body[k] = form.value[k]
    }
    await api.post('/health/metrics', body)
    ElMessage.success('已保存')
    entryVisible.value = false
    await loadAll()
  } catch (e) { ElMessage.error(e.message || '保存失败') } finally { saving.value = false }
}

async function loadAll() {
  loading.value = true
  loadError.value = ''
  try {
    // 指标是主数据（失败整页报错）；状态速览与打卡趋势是辅助视图，各自失败只降级为缺席
    const [metricsData, ov, hist] = await Promise.all([
      api.get('/health/metrics', { days: rangeDays }),
      api.get('/health/overview').catch(() => null),
      api.get('/checkin/history', { days: 30 }).catch(() => ({ items: [] })),
    ])
    d.value = metricsData
    overview.value = ov
    checkinItems.value = hist?.items || []
    loading.value = false   // 图表容器在 v-else-if 分支里，必须先撤骨架屏它才会进入 DOM
    if (d.value.empty) return
    await nextTick()        // v-if 包裹的图表容器需等 DOM 挂载后再 init
    renderCharts()
    renderCheckinChart()
  } catch (e) {
    loadError.value = e.message || '加载失败，请确认后端服务正在运行'
  } finally { loading.value = false }
}

onMounted(loadAll)
onUnmounted(charts.disposeAll)
</script>

<style scoped>
.head-row { display: flex; align-items: flex-start; justify-content: space-between; }
.metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.metric { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; padding: 13px 14px 11px; position: relative; overflow: hidden; }
.metric::after { content: ''; position: absolute; inset: 0 0 auto 0; height: 2px; background: linear-gradient(90deg, var(--cyan), transparent 70%); opacity: .7; }
.m-val { font-size: 27px; color: var(--text); }
.m-sub { font-size: 13px; color: var(--text-3); margin-left: 2px; }
.m-label { font-size: 11px; color: var(--text-3); margin-top: 5px; letter-spacing: .04em; }
.m-note { font-size: 11px; color: var(--text-2); margin-top: 3px; }
.card-head { display: flex; align-items: baseline; gap: 10px; }
.card-sub { font-size: 12px; color: var(--text-3); font-weight: 400; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; }
.ov-strip { display: flex; align-items: center; gap: 18px; margin-bottom: 12px; padding: 10px 16px;
  background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; justify-content: space-between; }
.ov-item { display: flex; flex-direction: column; gap: 2px; }
.ov-k { font-size: 11px; color: var(--text-3); letter-spacing: .03em; }
.ov-v { font-size: 18px; font-weight: 700; color: var(--text); }
.ov-u { font-size: 11px; color: var(--text-3); margin-left: 2px; font-weight: 400; }
.ov-s { font-size: 11px; color: var(--text-2); }
.ov-div { width: 1px; height: 30px; background: var(--border); flex-shrink: 0; }
</style>
