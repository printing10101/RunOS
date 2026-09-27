<template>
  <div v-if="d">
    <div class="detail-head">
      <div class="head-left">
        <el-button round plain size="small" @click="$router.push('/activities')">← 返回列表</el-button>
        <h1 class="page-title" style="margin:0">{{ d.title }}</h1>
      </div>
      <div class="head-chips">
        <el-tag effect="plain" round>{{ dateStr }}</el-tag>
        <el-tag effect="plain" round type="info">{{ platName }}</el-tag>
        <el-tag v-if="d.weather" effect="plain" round type="warning">{{ d.weather }} · {{ d.temp_c }}℃</el-tag>
        <el-tag v-if="isRace" effect="dark" round class="race-chip">比赛</el-tag>
        <el-button size="small" round plain style="margin-left:10px" @click="openEdit">编辑</el-button>
        <el-button size="small" round plain type="danger" @click="delActivity">删除</el-button>
      </div>
    </div>

    <!-- 编辑弹窗：修正录错/同步有误的指标 -->
    <el-dialog v-model="editVisible" title="编辑训练" width="480px">
      <el-form :model="editForm" label-width="96px" size="default">
        <div class="form-grid">
          <el-form-item label="运动类型">
            <el-select v-model="editForm.sport" style="width:100%">
              <el-option v-for="s in ['run', 'ride', 'swim', 'strength', 'walk', 'other']" :key="s"
                         :value="s" :label="{ run: '跑步', ride: '骑行', swim: '游泳', strength: '力量', walk: '步行', other: '其他' }[s]" />
            </el-select>
          </el-form-item>
          <el-form-item label="标题"><el-input v-model="editForm.title" placeholder="如：晨跑" /></el-form-item>
          <el-form-item label="日期时间">
            <el-date-picker v-model="editForm.start_time" type="datetime" format="YYYY-MM-DD HH:mm"
                            value-format="YYYY-MM-DD HH:mm:ss"
                            date-format="YYYY-MM-DD" time-format="HH:mm" style="width:100%" />
          </el-form-item>
          <el-form-item label="时长(分钟)"><el-input-number v-model="editForm.duration_min" :min="1" :max="600" controls-position="right" style="width:100%" /></el-form-item>
          <el-form-item label="距离(km)"><el-input-number v-model="editForm.distance_km" :min="0" :max="500" :precision="2" controls-position="right" style="width:100%" /></el-form-item>
          <el-form-item label="平均心率"><el-input-number v-model="editForm.avg_hr" :min="60" :max="220" controls-position="right" style="width:100%" /></el-form-item>
          <el-form-item label="最大心率"><el-input-number v-model="editForm.max_hr" :min="80" :max="230" controls-position="right" style="width:100%" /></el-form-item>
          <el-form-item label="步频(spm)"><el-input-number v-model="editForm.avg_cadence" :min="0" :max="240" controls-position="right" style="width:100%" /></el-form-item>
          <el-form-item label="爬升(m)"><el-input-number v-model="editForm.elevation_m" :min="0" :max="9999" controls-position="right" style="width:100%" /></el-form-item>
          <el-form-item label="卡路里"><el-input-number v-model="editForm.calories" :min="0" :max="9999" controls-position="right" style="width:100%" /></el-form-item>
        </div>
      </el-form>
      <template #footer>
        <el-button round @click="editVisible = false">取消</el-button>
        <el-button type="primary" round :loading="saving" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>

    <el-alert v-for="(n, i) in d.notes" :key="i" :title="n" type="info" :closable="false"
              style="margin-bottom: 12px" />

    <!-- 指标卡 -->
    <div class="metric-grid">
      <div class="metric"><div class="num-display m-val">{{ km }}</div><div class="m-label">距离 km</div></div>
      <div class="metric"><div class="num-display m-val">{{ timeStr }}</div><div class="m-label">时长</div></div>
      <div class="metric"><div class="num-display m-val">{{ pace }}</div><div class="m-label">平均配速</div></div>
      <div class="metric" v-if="d.avg_hr">
        <div class="num-display m-val">{{ d.avg_hr }}<span class="m-sub">/{{ d.max_hr }}</span></div><div class="m-label">心率 bpm</div>
      </div>
      <div class="metric" v-if="d.avg_cadence"><div class="num-display m-val">{{ Math.round(d.avg_cadence) }}</div><div class="m-label">步频 spm</div></div>
      <div class="metric"><div class="num-display m-val">{{ Math.round(d.elevation_m) }}</div><div class="m-label">累计爬升 m</div></div>
      <div class="metric" v-if="d.calories"><div class="num-display m-val">{{ d.calories }}</div><div class="m-label">卡路里 kcal</div></div>
      <div class="metric" v-if="d.avg_power"><div class="num-display m-val">{{ d.avg_power }}</div><div class="m-label">平均功率 W</div></div>
      <div class="metric" v-else-if="d.estimated_power">
        <div class="num-display m-val">{{ d.estimated_power.watts }}<span class="m-sub"> · {{ d.estimated_power.w_per_kg }}W/kg</span></div>
        <div class="m-label">{{ d.estimated_power.basis }}</div>
      </div>
      <div class="metric"><div class="num-display m-val">{{ Math.round(d.training_load) }}</div><div class="m-label">训练负荷</div></div>
      <div class="metric" v-if="d.te_aerobic != null">
        <div class="num-display m-val">{{ d.te_aerobic.toFixed(1) }}<span v-if="d.te_anaerobic != null" class="m-sub"> / {{ d.te_anaerobic.toFixed(1) }}</span></div>
        <div class="m-label">训练效果 有氧/无氧</div>
      </div>
      <div class="metric" v-if="d.rpe"><div class="num-display m-val">{{ d.rpe }}</div><div class="m-label">RPE 主观强度</div></div>
    </div>

    <!-- 关联课表课的 AI 教练点评 -->
    <el-card shadow="never" style="margin-top:14px" v-if="d.plan_workout">
      <template #header>
        <div class="card-head" style="display:flex; align-items:center; gap:8px">
          <span>AI 教练点评
            <span class="card-sub">课表课「{{ d.plan_workout.title }}」 · {{ d.plan_workout.date }}</span>
          </span>
          <el-button size="small" link type="primary" style="margin-left:auto"
                     :loading="commentLoading"
                     @click="makeComment(!!d.plan_workout.coach_comment)">
            {{ d.plan_workout.coach_comment ? '重新点评' : '生成点评' }}
          </el-button>
        </div>
      </template>
      <div v-if="d.plan_workout.coach_comment" class="coach-comment">{{ d.plan_workout.coach_comment }}</div>
      <div v-else class="coach-empty">
        还没有点评<template v-if="d.plan_workout.status !== 'completed'">（该课尚未标记完成，完成打卡后才能生成）</template>
      </div>
    </el-card>

    <!-- 跑步动态 -->
    <el-card shadow="never" style="margin-top:14px" v-if="dynCards.length">
      <template #header><div class="card-head">跑步动态</div></template>
      <div class="dyn-grid">
        <div v-for="c in dynCards" :key="c.label" class="dyn-item">
          <div class="num-display dyn-val">{{ c.value }}<span class="dyn-unit">{{ c.unit }}</span></div>
          <div class="m-label">{{ c.label }}</div>
          <div class="dyn-note">{{ c.note }}</div>
        </div>
      </div>
    </el-card>
    <el-card shadow="never" style="margin-top:14px" v-if="extrasText">
      <template #header><div class="card-head">专项数据</div></template>
      <div class="extras">{{ extrasText }}</div>
    </el-card>

    <el-row :gutter="14" style="margin-top:14px">
      <el-col :span="16">
        <el-card shadow="never">
          <template #header><div class="card-head">配速 · GAP · 心率 <span class="card-sub">按公里</span></div></template>
          <div ref="mainChart" style="height: 320px"></div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" v-if="d.hr_zone_times">
          <template #header><div class="card-head">心率区间时间分布</div></template>
          <div ref="zoneChart" style="height: 300px"></div>
        </el-card>
        <el-card shadow="never" v-if="d.track" style="margin-top:14px">
          <template #header><div class="card-head">GPS 轨迹 <span class="card-sub">{{ (d.track.length - 1) }} 点</span></div></template>
          <div ref="trackChart" style="height: 300px"></div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="14" style="margin-top:14px">
      <el-col :span="8">
        <el-card shadow="never" v-if="altPoints.length">
          <template #header><div class="card-head">海拔剖面 <span class="card-sub">累计爬升 {{ Math.round(d.elevation_m) }} m</span></div></template>
          <div ref="altChart" style="height: 240px"></div>
        </el-card>
      </el-col>
      <el-col :span="16">
        <el-card shadow="never" v-if="d.splits.items.length">
          <template #header>
            <div class="card-head">每公里分段
              <span class="card-sub">{{ d.splits.source === 'device' ? '设备实测' : '均值模拟' }}
                <template v-if="d.splits.gap_available"> · GAP 为坡度调整配速</template>
              </span>
            </div>
          </template>
          <el-table :data="d.splits.items" size="small" max-height="360">
            <el-table-column label="km" width="55">
              <template #default="{ row }">{{ row.index }}</template>
            </el-table-column>
            <el-table-column label="配速" align="right">
              <template #default="{ row }">
                <span :style="{ color: paceColor(row.pace_sec_per_km), fontWeight: 700 }">{{ fmtPace(row.pace_sec_per_km) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="GAP" align="right" v-if="d.splits.gap_available">
              <template #default="{ row }">{{ row.gap_sec_per_km ? fmtPace(row.gap_sec_per_km) : '-' }}</template>
            </el-table-column>
            <el-table-column label="坡度" width="70" align="right" v-if="d.splits.gap_available">
              <template #default="{ row }">{{ row.grade != null ? row.grade + '%' : '-' }}</template>
            </el-table-column>
            <el-table-column label="均心率" width="80" align="right">
              <template #default="{ row }">{{ row.avg_hr ?? '-' }}</template>
            </el-table-column>
            <el-table-column label="爬升" width="70" align="right">
              <template #default="{ row }">{{ row.elev_gain_m ? '+' + row.elev_gain_m + 'm' : '-' }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
  <div v-else-if="loadError" style="padding:60px 0; text-align:center">
    <el-empty :description="loadError">
      <el-button type="primary" round @click="loadActivity">重试</el-button>
    </el-empty>
  </div>
  <div v-else style="padding:40px; text-align:center">
    <el-skeleton style="width:100%" :rows="8" animated />
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as echarts from 'echarts'
import { api, fmtPace, fmtTime } from '../api'
import { platName as platNameOf, localDateTimeStr } from '../utils/common'
import { axisStyle, tooltipStyle } from '../utils/echarts-theme'
import { createChartManager } from '../composables/charts'

const route = useRoute()
const router = useRouter()
const d = ref(null)
const loadError = ref('')
const mainChart = ref(null)
const zoneChart = ref(null)
const trackChart = ref(null)
const altChart = ref(null)

// 编辑/删除
const editVisible = ref(false)
const saving = ref(false)
const editForm = ref({})

function openEdit() {
  const v = d.value
  editForm.value = {
    sport: v.sport, title: v.title,
    start_time: v.start_time || localDateTimeStr(new Date()),
    duration_min: v.duration_sec ? Math.round(v.duration_sec / 60) : 1,
    distance_km: v.distance_m ? Math.round(v.distance_m / 10) / 100 : 0,
    avg_hr: v.avg_hr ?? null, max_hr: v.max_hr ?? null,
    avg_cadence: v.avg_cadence ?? null, elevation_m: v.elevation_m ?? 0,
    calories: v.calories ?? null,
  }
  editVisible.value = true
}

async function saveEdit() {
  const f = editForm.value
  if (!f.start_time || !f.duration_min) { ElMessage.warning('请填写日期与时长'); return }
  saving.value = true
  try {
    await api.put(`/activities/${route.params.id}`, {
      sport: f.sport, title: f.title || '训练', start_time: f.start_time,
      duration_sec: Math.round(f.duration_min * 60), distance_m: Math.round((f.distance_km || 0) * 1000),
      avg_hr: f.avg_hr || null, max_hr: f.max_hr || null, avg_cadence: f.avg_cadence || null,
      elevation_m: f.elevation_m || 0, calories: f.calories || null,
    })
    editVisible.value = false
    ElMessage.success('已更新')
    loadActivity()
  } catch (e) {
    ElMessage.error(e.message || '更新失败')
  } finally { saving.value = false }
}

async function delActivity() {
  try {
    await ElMessageBox.confirm(
      `确定删除「${d.value.title || '该训练'}」（${d.value.start_time?.slice(0, 10)}）？删除后不可恢复`,
      '删除训练', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' })
  } catch { return }
  try {
    await api.del(`/activities/${route.params.id}`)
    ElMessage.success('已删除')
    router.push('/activities')
  } catch (e) {
    ElMessage.error(e.message || '删除失败')
  }
}

// 训练后 AI 点评（关联课表课时展示）
const commentLoading = ref(false)

async function makeComment(force) {
  commentLoading.value = true
  try {
    const r = await api.post('/ai/workout-comment',
                             { workout_id: d.value.plan_workout.workout_id, force })
    d.value.plan_workout.coach_comment = r.comment
    d.value.plan_workout.coach_comment_at = r.at
    ElMessage.success(r.source === 'rule' ? '已生成规则点评（本地模型未就绪）' : 'AI 点评已生成')
  } catch (e) {
    ElMessage.error(e.message || '点评生成失败')
  } finally { commentLoading.value = false }
}

const km = computed(() => (d.value?.distance_m / 1000).toFixed(2))
const timeStr = computed(() => fmtTime(d.value?.duration_sec))
const pace = computed(() => d.value?.pace_sec_per_km ? fmtPace(d.value.pace_sec_per_km) : '-')
const dateStr = computed(() => d.value?.start_time?.slice(0, 16).replace('T', ' '))
const isRace = computed(() => d.value?.title?.includes('比赛'))
// 平台名统一走 utils/common 的映射（此前的内联副本缺 strava，标签会漏出英文小写）
const platName = computed(() => platNameOf(d.value?.platform))

const dynCards = computed(() => {
  const dyn = d.value?.dynamics || {}
  const cards = []
  if (dyn.stride_m) cards.push({ label: '步幅', value: dyn.stride_m.toFixed(2), unit: 'm', note: '1.1m 以上为优秀' })
  if (dyn.vosc_cm) cards.push({ label: '垂直振幅', value: dyn.vosc_cm.toFixed(1), unit: 'cm', note: '≤8cm 更经济' })
  if (dyn.gct_ms) cards.push({ label: '触地时间', value: dyn.gct_ms, unit: 'ms', note: '≤270ms 更快' })
  if (dyn.gct_balance_pct) {
    const b = dyn.gct_balance_pct
    cards.push({ label: '左右触地平衡', value: (b < 50 ? b.toFixed(1) + ' / ' + (100 - b).toFixed(1) : (100 - b).toFixed(1) + ' / ' + b.toFixed(1)), unit: '%', note: '越接近 50/50 越均衡' })
  }
  return cards
})
const extrasText = computed(() => {
  const ex = d.value?.extras
  if (!ex) return ''
  const parts = []
  if (ex.strokes_per_length) parts.push(`每趟划次 ${ex.strokes_per_length}`)
  if (ex.swolf) parts.push(`SWOLF ${ex.swolf}`)
  if (ex.pool_length_m) parts.push(`泳长 ${ex.pool_length_m}m`)
  if (ex.avg_rpm) parts.push(`踏频 ${Math.round(ex.avg_rpm)} rpm`)
  if (ex.avg_power_w) parts.push(`功率 ${Math.round(ex.avg_power_w)}W`)
  return parts.join(' · ')
})

const altPoints = computed(() => (d.value?.series?.points || []).filter(p => p.altitude_m != null))

function paceColor(p) {
  if (!d.value?.pace_sec_per_km || !p) return '#e8eef6'
  const diff = d.value.pace_sec_per_km - p
  if (diff > 15) return '#ff6b6b'   // 明显快于平均
  if (diff > 5) return '#ffa24d'
  if (diff < -15) return '#5b9dff'
  return '#e8eef6'
}

// 统一管理图表实例：init 去重、窗口缩放自动 resize、卸载时 dispose
const charts = createChartManager()

function renderCharts() {
  charts.clear()   // 切换活动后全量重绘，避免旧 series 残留
  const pts = d.value?.series?.points || []
  if (mainChart.value && pts.length) {
    const hasHr = pts.some(p => p.hr)
    // GAP 覆盖线定位在分段中点（按累计里程）
    let acc = 0
    const gapSeries = (d.value.splits.items || []).filter(s => s.gap_sec_per_km)
      .map(s => {
        const mid = acc + s.distance_m / 2000
        acc += s.distance_m
        return [Math.round(mid * 100) / 100, s.gap_sec_per_km]
      })
    const lastKm = pts[pts.length - 1].km
    const chart = charts.get('main', mainChart.value)
    chart.setOption({
      grid: { left: 52, right: hasHr ? 46 : 16, top: 30, bottom: 30 },
      legend: { textStyle: { color: '#9aa8ba' }, top: 0, itemWidth: 14 },
      tooltip: { trigger: 'axis', ...tooltipStyle,
        valueFormatter: v => (typeof v === 'number' && v > 300) ? fmtPace(v) : v },
      xAxis: { type: 'value', min: 0, max: Math.ceil(lastKm * 10) / 10, ...axisStyle,
        axisLabel: { ...axisStyle.axisLabel, formatter: v => Math.round(v) + 'k' } },
      yAxis: [
        { type: 'value', inverse: true, ...axisStyle, splitLine: { lineStyle: { color: 'rgba(148,163,184,.08)' } },
          axisLabel: { ...axisStyle.axisLabel, formatter: v => fmtPace(v) } },
        ...(hasHr ? [{ type: 'value', ...axisStyle, splitLine: { show: false }, min: v => Math.floor(v.min / 10) * 10 - 10 }] : []),
      ],
      series: [
        { name: '配速', type: 'line', data: pts.map(p => [p.km, p.pace_sec_per_km]), smooth: true, symbol: 'none',
          lineStyle: { color: '#c8f169', width: 2.5 }, yAxisIndex: 0 },
        ...(gapSeries.length ? [{ name: 'GAP（坡度调整）', type: 'line', smooth: true, symbol: 'none',
          data: gapSeries,
          lineStyle: { color: '#56d4e0', width: 1.6, type: 'dashed' }, yAxisIndex: 0 }] : []),
        ...(hasHr ? [{ name: '心率', type: 'line', data: pts.map(p => [p.km, p.hr]), smooth: true, symbol: 'none',
          lineStyle: { color: '#ff6b6b', width: 1.8 }, yAxisIndex: 1 }] : []),
      ],
    })
  }
  if (altChart.value && altPoints.value.length) {
    const alt = charts.get('alt', altChart.value)
    alt.setOption({
      grid: { left: 44, right: 10, top: 14, bottom: 24 },
      tooltip: { trigger: 'axis', ...tooltipStyle },
      xAxis: { type: 'value', min: 0, ...axisStyle,
        axisLabel: { ...axisStyle.axisLabel, formatter: v => Math.round(v) + 'k' } },
      yAxis: { type: 'value', ...axisStyle, min: v => Math.floor(v.min - 5), splitLine: { show: false } },
      series: [{ type: 'line', data: altPoints.value.map(p => [p.km, p.altitude_m]), smooth: true, symbol: 'none',
        lineStyle: { color: '#ffa24d', width: 1.8 },
        areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(255,162,77,.35)' }, { offset: 1, color: 'rgba(255,162,77,.02)' }]) } }],
    })
  }
  if (zoneChart.value && d.value.hr_zone_times) {
    const zc = charts.get('zone', zoneChart.value)
    const zoneMeta = [['Z1', '恢复', '#7d8ea3'], ['Z2', '耐力', '#4ade80'], ['Z3', '有氧', '#56d4e0'],
                      ['Z4', '阈值', '#ffa24d'], ['Z5', '无氧', '#ff6b6b']]
    const items = zoneMeta.filter(([k]) => d.value.hr_zone_times[k]).map(([k, label, color]) => ({
      key: k, label, color, sec: d.value.hr_zone_times[k] }))
    zc.setOption({
      grid: { left: 74, right: 48, top: 10, bottom: 24 },
      tooltip: { trigger: 'axis', ...tooltipStyle, valueFormatter: v => `${Math.round(v / 60)} 分钟` },
      xAxis: { type: 'value', ...axisStyle, axisLabel: { ...axisStyle.axisLabel, formatter: v => Math.round(v / 60) + '分' } },
      yAxis: { type: 'category', data: items.map(i => `${i.key} ${i.label}`), ...axisStyle },
      series: [{ type: 'bar', data: items.map(i => ({ value: i.sec, itemStyle: { color: i.color, borderRadius: [0, 4, 4, 0] } })),
        barWidth: '55%', label: { show: true, position: 'right', color: '#9aa8ba', fontSize: 11,
          formatter: p => `${Math.round(p.value / 60)}′` } }],
    })
  }
  if (trackChart.value && d.value.track?.length) {
    const track = d.value.track
    const lats = track.map(p => p[0]), lngs = track.map(p => p[1])
    const tc = charts.get('track', trackChart.value)
    tc.setOption({
      grid: { left: 8, right: 8, top: 8, bottom: 8 },
      tooltip: { trigger: 'item', ...tooltipStyle },
      xAxis: { type: 'value', min: Math.min(...lngs) - 0.0008, max: Math.max(...lngs) + 0.0008, show: false },
      yAxis: { type: 'value', min: Math.min(...lats) - 0.0006, max: Math.max(...lats) + 0.0006, show: false },
      series: [
        { type: 'line', data: track.map(p => [p[1], p[0]]), symbol: 'none', smooth: true,
          lineStyle: { color: '#c8f169', width: 2.4 },
          areaStyle: { color: 'rgba(200,241,105,0.05)' },
          markPoint: { symbolSize: 9, label: { show: false },
            data: [{ coord: track[0].slice().reverse(), itemStyle: { color: '#4ade80' } },
                   { coord: track[track.length - 1].slice().reverse(), itemStyle: { color: '#ff6b6b' } }] } },
      ],
    })
  }
}

let loadSeq = 0
function loadActivity() {
  const seq = ++loadSeq
  d.value = null
  loadError.value = ''
  api.get(`/activities/${route.params.id}`).then(v => {
    if (seq !== loadSeq) return   // 已有更新的请求发出，丢弃过期响应（防快速连点竞态）
    d.value = v
    nextTick(renderCharts)
  }).catch(e => {
    if (seq !== loadSeq) return
    loadError.value = e.message || '加载失败，请确认后端服务正在运行'
  })
}

onMounted(loadActivity)
// 同一组件内 :id 变化（列表连续点击 / 浏览器前进后退）时重新加载，避免停留旧活动
watch(() => route.params.id, loadActivity)
// 卸载时释放全部图表实例，避免实例累积
onUnmounted(charts.disposeAll)
</script>

<style scoped>
.detail-head { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 16px; flex-wrap: wrap; gap: 10px; }
.head-left { display: flex; align-items: center; gap: 12px; }
.head-chips .el-tag { margin-left: 6px; }
.race-chip { background: rgba(245, 194, 107, 0.18); color: var(--gold); border-color: rgba(245, 194, 107, 0.4); font-weight: 700; }

.metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(112px, 1fr)); gap: 10px; }
.metric { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; padding: 13px 14px 11px; position: relative; overflow: hidden; }
.metric::after { content: ''; position: absolute; inset: 0 0 auto 0; height: 2px; background: linear-gradient(90deg, var(--lime), transparent 70%); opacity: .7; }
.m-val { font-size: 25px; color: var(--text); }
.m-sub { font-size: 13px; color: var(--text-3); margin-left: 1px; }
.m-label { font-size: 11px; color: var(--text-3); margin-top: 5px; letter-spacing: .04em; }


.dyn-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; }
.dyn-val { font-size: 27px; color: var(--text); }
.dyn-unit { font-size: 12px; color: var(--text-3); margin-left: 3px; }
.dyn-note { font-size: 11px; color: var(--text-3); margin-top: 3px; }
.extras { color: var(--text-2); font-size: 13.5px; }
.coach-comment { color: var(--text-2); font-size: 13.5px; line-height: 1.85;
  border-left: 3px solid rgba(200, 241, 105, 0.5); padding-left: 12px; }
.coach-empty { color: var(--text-3); font-size: 13px; }
</style>
