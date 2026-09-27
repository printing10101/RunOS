<template>
  <div>
    <el-skeleton v-if="loading" style="margin-top:8px" :rows="10" animated />
    <el-alert v-else-if="loadError" type="error" show-icon :closable="false" style="margin-top:8px"
              title="数据加载失败" :description="loadError">
      <el-button size="small" text bg @click="retryDashboard">重试</el-button>
    </el-alert>
    <el-empty v-else-if="d.empty" description="还没有个人档案，先完善档案，再录入或同步真实训练数据">
      <el-button type="primary" round @click="$router.push('/profile')">去完善个人档案</el-button>
    </el-empty>
    <template v-else>
    <div class="today-head">
      <div>
        <h1 class="page-title">{{ greeting }}，{{ d.athlete?.name }}</h1>
        <div class="page-sub">{{ todayText }}</div>
      </div>
      <div class="head-chips">
        <el-tag effect="plain" round>{{ raceCountdown }}</el-tag>
        <el-tag v-if="d.plan_week" effect="dark" round class="phase-chip">
          第 {{ d.plan_week.week_index }} 周 · {{ phaseName(d.plan_week.phase) }}
        </el-tag>
        <!-- 计划尚未进入首周时（今天早于 start_date），明示开始日期，
             避免用户把「没有周次」误读成日期识别失败 -->
        <el-tag v-else-if="d.plan?.start_date" effect="plain" round class="phase-chip">
          计划 {{ fmtDate(d.plan.start_date) }} 开始
        </el-tag>
        <el-tag v-if="syncStale" effect="plain" round type="warning" class="sync-stale"
                @click="$router.push('/connections')">⇡ {{ syncStale }}</el-tag>
      </div>
    </div>

    <!-- 训练状态条：状态标签 / 准备度 / 恢复时间 -->
    <div class="status-strip" v-if="d.status_summary">
      <div class="ss-item">
        <span class="dot" :style="{ background: statusColor }"></span>
        <span class="ss-k">训练状态</span>
        <span class="ss-v">{{ d.status_summary.status }}</span>
      </div>
      <div class="ss-div"></div>
      <div class="ss-item">
        <span class="ss-k">训练准备度</span>
        <span class="num-display ss-v" :style="{ color: readinessColor }"><RollNum :value="d.status_summary.readiness" /></span>
      </div>
      <div class="ss-div"></div>
      <div class="ss-item">
        <span class="ss-k">恢复剩余</span>
        <span class="num-display ss-v"><RollNum :value="d.status_summary.recovery_time_h" fallback="0" /><span class="ss-u">h</span></span>
        <span class="ss-k" v-if="d.status_summary.official_recovery"
              :title="`高驰官方恢复 ${d.status_summary.official_recovery.pct}%${d.status_summary.official_recovery.level ? ' · ' + d.status_summary.official_recovery.level : ''}`">
          官方 {{ d.status_summary.official_recovery.pct }}%
        </span>
      </div>
      <div class="ss-div"></div>
      <div class="ss-item">
        <span class="ss-k">ACWR</span>
        <span class="num-display ss-v"><RollNum :value="d.status_summary.acwr" /></span>
      </div>
      <el-button size="small" round text class="ss-more" @click="$router.push('/status')">查看训练状态 →</el-button>
    </div>

    <el-row :gutter="14">
      <!-- 今日训练 HERO -->
      <el-col :span="16">
        <el-card shadow="never" class="hero" :class="'hero-' + (d.next_workout?.session_type || 'rest')">
          <template #header>
            <div class="hero-head">
              <span class="hero-label">TODAY'S WORKOUT · 今日训练</span>
              <el-tag v-if="d.next_workout" :color="typeStyle.color" effect="dark" style="border: none; font-weight:700">
                {{ typeStyle.label }}
              </el-tag>
              <el-tag v-if="d.next_workout?.plan_source === 'coros'" type="warning" effect="plain"
                      style="font-weight:700">⌚ 高驰课表</el-tag>
            </div>
          </template>
          <template v-if="d.next_workout">
            <div class="hero-title">{{ d.next_workout.title }}</div>
            <div class="hero-metrics">
              <div class="hm">
                <div class="num-display hm-num"><RollNum :value="d.next_workout.distance_km || null" /></div>
                <div class="hm-label">公里</div>
              </div>
              <div class="hm-div"></div>
              <div class="hm">
                <div class="num-display hm-num"><RollNum :value="d.next_workout.duration_min || null" /></div>
                <div class="hm-label">分钟</div>
              </div>
              <div class="hm-div"></div>
              <div class="hm">
                <div class="num-display hm-num">{{ hrHint }}</div>
                <div class="hm-label">强度区间</div>
              </div>
            </div>

            <div class="hero-steps" v-if="d.next_workout.structured?.length">
              <div v-for="(s, i) in d.next_workout.structured" :key="i" class="hstep">
                <span class="hstep-dot" :style="{ background: stepColor(s.step_type) }"></span>
                <span class="hstep-name">{{ s.name }}</span>
                <span class="hstep-target">{{ s.target?.label || '' }}</span>
              </div>
              <div class="hstep-more" v-if="stepsTotal > 4">…共 {{ stepsTotal }} 个步骤</div>
            </div>

            <div class="hero-tip" v-if="d.next_workout.diet_tip">🥗 {{ d.next_workout.diet_tip }}</div>

            <div class="hero-actions" v-if="d.next_workout.plan_source !== 'coros'">
              <el-button type="primary" round :loading="pushingId === d.next_workout.id" @click="pushWo(d.next_workout)">
                {{ d.next_workout.pushed_platforms?.length ? '再次下发' : '下发到手表' }}
              </el-button>
              <el-button round plain @click="downloadFit(d.next_workout)">导出 FIT</el-button>
              <el-tag v-for="p in d.next_workout.pushed_platforms || []" :key="p" size="small" type="success"
                      effect="plain" style="margin-left:8px">✓ 已同步 {{ platName(p) }}</el-tag>
            </div>
            <div class="hero-tip" v-else>课表来自高驰官方同步，请直接按手表上的课表执行</div>
          </template>
          <el-empty v-else description="今天没有安排训练，好好恢复" :image-size="70" />
        </el-card>

        <!-- 训练配速带 -->
        <el-card shadow="never" style="margin-top:14px">
          <template #header>
            <div class="card-head">训练配速带 <span class="card-sub">VDOT {{ d.current_vdot }} · Daniels 体系</span></div>
          </template>
          <div class="pace-strip">
            <div v-for="z in d.paces" :key="z.key" class="pace-tile" :class="'pt-' + z.key">
              <div class="pt-key">{{ z.label.split(' ')[0] }}</div>
              <div class="pt-name">{{ z.label.split(' ')[1] }}</div>
              <div class="pt-pace num-display">{{ z.pace_from }}</div>
              <div class="pt-to">~ {{ z.pace_to }}</div>
            </div>
          </div>
        </el-card>
      </el-col>

      <!-- 右列：数据环 + 跑者类型 -->
      <el-col :span="8">
        <el-card shadow="never">
          <template #header><div class="card-head">身体与负荷</div></template>
          <div class="rings">
            <RingGauge :value="weekPct" :label="d.this_week?.km ?? '-'" :digits="1" unit="km"
                  sub="本周跑量" color="#c8f169" />
            <RingGauge :value="vdotPct" :label="d.current_vdot ?? '-'" sub="VDOT" color="#5b9dff" />
            <RingGauge :value="d.assessment?.total_score || 0" :label="d.assessment?.total_score || '-'" sub="综合分"
                  :sub2="d.assessment?.grade ? d.assessment.grade + ' 级' : ''" color="#ffa24d" />
          </div>
          <div class="ring-foot" v-if="d.this_week">
            本周已练 {{ d.this_week.sessions }} 次 · {{ d.this_week.hours }}h<span
              v-if="d.plan_week"> · 目标 {{ d.plan_week.target_km }}km（{{ weekPct }}%）</span>
          </div>
        </el-card>

        <div style="margin-top:14px"><CheckinCard ref="checkinCardRef" /></div>

        <el-card shadow="never" style="margin-top:14px" v-if="d.runner_type && d.runner_type.event !== 'unknown'">
          <template #header><div class="card-head">跑者类型</div></template>
          <div class="rt-row">
            <div class="rt-icon">{{ rtIcon }}</div>
            <div>
              <div style="font-weight:800; font-size:16px">{{ d.runner_type.type_name }}
                <span style="font-weight:400; color:var(--text-3); font-size:12px; margin-left:4px">{{ d.runner_type.level }}</span>
              </div>
              <div style="font-size:12px; color:var(--text-2); margin-top:4px; line-height:1.7">
                {{ d.runner_type.description }}
              </div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="14" style="margin-top:14px">
      <el-col :span="16">
        <el-card shadow="never">
          <template #header><div class="card-head">近 12 周跑量 <span class="card-sub">km / 周</span></div></template>
          <div ref="kmChart" style="height: 240px"></div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never">
          <template #header><div class="card-head">最近训练 <span class="card-sub">点击查看详情</span></div></template>
          <div v-for="a in d.recent_activities" :key="a.id" class="recent-row" @click="$router.push(`/activities/${a.id}`)">
            <span class="dot" :style="{ background: kindColor(a.kind || a.session_type || a.sport) }"></span>
            <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--text-2)">{{ a.title }}</span>
            <span class="recent-meta">{{ fmtDate(a.start_time) }} · {{ a.distance_km }}km
              <template v-if="a.pace_sec_per_km"> · {{ fmtPace(a.pace_sec_per_km) }}</template></span>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 每周自动复盘（跑后环节） -->
    <WeeklyRecap ref="weeklyRecapRef" />
    </template>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import * as echarts from 'echarts'
import { api, fmtPace, fmtDate, SESSION_STYLE } from '../api'
import RingGauge from '../components/RingGauge.vue'
import RollNum from '../components/RollNum.vue'
import CheckinCard from '../components/CheckinCard.vue'
import WeeklyRecap from '../components/WeeklyRecap.vue'
import { platName, phaseName, stepColor, kindColor, downloadFile, todayStr, daysBetween } from '../utils/common'
import { axisStyle, tooltipStyle, splitLineStyle } from '../utils/echarts-theme'
import { createChartManager } from '../composables/charts'

const d = ref({})
const kmChart = ref(null)
// 打卡与周复盘拆成了子组件（CheckinCard / WeeklyRecap），自含状态，
// 父组件只在首载和轮询时调它们的 load 方法
const checkinCardRef = ref(null)
const weeklyRecapRef = ref(null)
const loading = ref(true)
const pushingId = ref(null)
// 数据新鲜度：已连接平台但 48h 未同步 / 从未同步时提醒
const syncStale = ref('')

async function loadFreshness() {
  try {
    const conn = await api.get('/connections')
    const cs = (conn.connections || []).filter(c => c.status === 'connected')
    if (!cs.length) { syncStale.value = ''; return }
    const times = cs.map(c => c.last_sync_at).filter(Boolean).sort()
    if (!times.length) { syncStale.value = '设备数据未同步过'; return }
    const hours = (Date.now() - new Date(times[times.length - 1]).getTime()) / 3600000
    syncStale.value = hours >= 48 ? `设备数据已 ${Math.floor(hours / 24)} 天未同步` : ''
  } catch { /* 平台连接不可用时静默 */ }
}

const greeting = computed(() => {
  const h = new Date().getHours()
  return h < 11 ? '早上好' : h < 14 ? '中午好' : h < 19 ? '下午好' : '晚上好'
})
const todayText = computed(() => {
  const now = new Date()
  const wd = ['日', '一', '二', '三', '四', '五', '六'][now.getDay()]
  return `${now.getMonth() + 1} 月 ${now.getDate()} 日 · 星期${wd}`
})
const raceCountdown = computed(() => {
  if (!d.value.plan?.race_date) return '尚未设定比赛'
  // 按本地日历日相减：new Date('2026-09-20') 会被解析成 UTC 午夜，
  // 与「现在」相减会带半天误差，边界日显示的天数会差 1 天
  const days = Math.max(0, daysBetween(todayStr(), d.value.plan.race_date))
  return `距比赛 ${days} 天`
})
// 课型样式统一引用 SESSION_STYLE（含知识库二代课型 interval/tempo/fartlek/hill/race），
// 此前的四键内联映射会让二代计划的「下一课」卡片退化为通用「训练」标签
const typeStyle = computed(() =>
  SESSION_STYLE[d.value.next_workout?.session_type] || { label: '训练', color: '#5b9dff' })

const stepsTotal = computed(() => d.value.next_workout?.structured?.length || 0)
// 心率提示按课型分组：质量课全家族（含二代词汇）都是 Z4-Z5
const HR_HINT = {
  easy: 'Z2', recovery: 'Z2', long: 'Z2-Z3',
  quality: 'Z4-Z5', interval: 'Z4-Z5', tempo: 'Z4-Z5', fartlek: 'Z4-Z5', hill: 'Z4-Z5', race: 'Z4-Z5',
}
const hrHint = computed(() => HR_HINT[d.value.next_workout?.session_type] || '-')
const weekPct = computed(() => {
  const target = d.value.plan_week?.target_km
  if (!target || !d.value.this_week) return 0
  return Math.min(100, Math.round(d.value.this_week.km / target * 100))
})
const vdotPct = computed(() => (d.value.current_vdot ? Math.min(100, Math.max(0, (d.value.current_vdot - 30) / 40 * 100)) : 0))
const rtIcon = computed(() => ({ speed: '⚡', endurance: '🏔️', balanced: '⚖️' }[d.value.runner_type?.event] || '🏃'))

const statusColor = computed(() => {
  const s = d.value.status_summary?.status || ''
  return { '效率良好': '#c8f169', '巅峰期': '#c8f169', '维持状态': '#5b9dff', '恢复中': '#56d4e0',
           '效率不佳': '#ff6b6b', '负荷过高': '#ff6b6b', '训练中断': '#ffa24d' }[s] || '#5b9dff'
})
const readinessColor = computed(() => {
  const s = d.value.status_summary?.readiness
  if (s == null) return '#e8eef6'
  return s >= 80 ? '#4ade80' : s >= 60 ? '#ffa24d' : '#ff6b6b'
})

async function pushWo(wo) {
  pushingId.value = wo.id
  try {
    // 只发支持下发写入的平台（高驰 MCP 未开放训练写入，带上必然失败一项）
    const conn = await api.get('/connections')
    const opts = conn.connections
      .filter(c => c.status === 'connected' && ['garmin', 'strava'].includes(c.platform))
      .map(c => c.platform)
    if (!opts.length) { ElMessage.warning('请先在「平台连接」页绑定佳明 / Strava'); return }
    const r = await api.post(`/plan/workouts/${wo.id}/push`, { platforms: opts })
    if (!r.pushed.length) {
      ElMessage.error(r.results?.filter(x => !x.ok).map(x => `${platName(x.platform)}：${x.error}`).join('；') || '下发失败')
    } else {
      ElMessage.success(`已下发：${r.pushed.map(platName).join('、')}`)
    }
    d.value = await api.get('/dashboard')
  } catch (e) { ElMessage.error(e.message || '下发失败') } finally { pushingId.value = null }
}
async function downloadFit(wo) {
  await downloadFile(`/api/plan/workouts/${wo.id}/fit`, `workout_${wo.date}.fit`)
}

const charts = createChartManager()
function renderKmChart() {
  if (!kmChart.value) return
  const kmSeries = d.value.weekly_km_series || []
  if (!kmSeries.length) return
  charts.get('km', kmChart.value).setOption({
    grid: { left: 42, right: 12, top: 18, bottom: 26 },
    tooltip: { trigger: 'axis', ...tooltipStyle },
    xAxis: { type: 'category', data: kmSeries.map((_, i) => `W-${11 - i}`), ...axisStyle },
    yAxis: { type: 'value', ...axisStyle, splitLine: splitLineStyle },
    series: [{
      type: 'bar', barWidth: '46%', data: kmSeries,
      itemStyle: {
        borderRadius: [5, 5, 2, 2],
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: '#c8f169' }, { offset: 1, color: 'rgba(200,241,105,0.25)' },
        ]),
      },
    }],
  })
}

// 自动同步带来的新数据自动上屏：每 2 分钟静默刷新（仅页面可见时），体感「实时更新」
let refreshTimer = null

const loadError = ref('')

async function loadDashboard() {
  try {
    d.value = await api.get('/dashboard')
    loadError.value = ''
  } catch (e) {
    // 首屏失败不再白屏：给出错误态和重试入口
    loadError.value = e.message || '无法连接后端服务，请确认服务已启动'
  } finally {
    loading.value = false
  }
}

async function initDashboard() {
  await loadDashboard()
  if (loadError.value || d.value.empty) return   // 失败或空档案：无图表容器，不再初始化
  // 撤骨架屏只是改了响应式状态，跑量图容器要等一次 DOM flush 才真正挂载；
  // 不等 nextTick 就 renderKmChart 会让 ref 为 null 直接 return，图要等 2 分钟后的自刷新才补画
  await nextTick()
  checkinCardRef.value?.loadCheckin()
  weeklyRecapRef.value?.loadRecap()
  loadFreshness()
  renderKmChart()
  refreshTimer = setInterval(() => {
    if (document.visibilityState !== 'visible') return
    api.get('/dashboard').then(nd => {
      if (nd.empty) return
      d.value = nd
      renderKmChart()
      checkinCardRef.value?.loadCheckin()
    }).catch(() => {})
    weeklyRecapRef.value?.loadRecap()
    loadFreshness()
  }, 120000)
}

function retryDashboard() {
  loading.value = true
  initDashboard()
}

onMounted(initDashboard)
onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
  charts.disposeAll()
})
</script>

<style scoped>
.today-head { display: flex; align-items: flex-end; justify-content: space-between; }
.head-chips .el-tag { margin-left: 8px; }
.sync-stale { cursor: pointer; }
.sync-stale:hover { border-color: rgba(255, 162, 77, 0.6); }
.phase-chip { background: rgba(200, 241, 105, 0.14); color: var(--lime); border: 1px solid rgba(200, 241, 105, 0.3); font-weight: 700; }

.status-strip {
  display: flex; align-items: center; gap: 16px; margin-top: 12px; padding: 10px 16px;
  background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px;
}
.ss-item { display: flex; align-items: baseline; gap: 8px; }
.ss-k { font-size: 12px; color: var(--text-3); }
.ss-v { font-size: 17px; font-weight: 700; color: var(--text); }
.ss-u { font-size: 11px; color: var(--text-3); }
.ss-div { width: 1px; height: 20px; background: var(--border); }
.ss-more { margin-left: auto; color: var(--text-3); }
.ss-more:hover { color: var(--lime); }
.recent-row { cursor: pointer; }

.hero :deep(.el-card__header) { padding: 14px 20px 10px; }
.hero-head { display: flex; align-items: center; justify-content: space-between; }
.hero-label { font-size: 11px; letter-spacing: 0.18em; color: var(--text-3); font-weight: 700; }
.hero-title { font-size: 26px; font-weight: 800; letter-spacing: 0.01em; }
.hero-metrics { display: flex; align-items: center; gap: 22px; margin: 18px 0 6px; }
.hm-num { font-size: 44px; color: var(--text); }
.hm-label { font-size: 12px; color: var(--text-3); margin-top: 4px; letter-spacing: 0.1em; }
.hm-div { width: 1px; height: 42px; background: var(--border); }

.hero-steps { margin: 12px 0 4px; }
.hstep { display: flex; align-items: center; gap: 10px; padding: 5px 0; font-size: 13px; }
.hstep-dot { width: 8px; height: 8px; border-radius: 3px; }
.hstep-name { color: var(--text-2); }
.hstep-target { color: var(--text-3); margin-left: auto; font-family: var(--font-display); letter-spacing: .03em; }
.hstep-more { color: var(--text-3); font-size: 12px; padding-top: 4px; }
.hero-tip { font-size: 12.5px; color: var(--orange); background: rgba(255, 162, 77, 0.08); border-radius: 8px; padding: 7px 10px; margin-top: 8px; }
.hero-actions { margin-top: 14px; display: flex; align-items: center; }


.pace-strip { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; }
.pace-tile { border: 1px solid var(--border); border-radius: 12px; padding: 12px 12px 10px; background: var(--bg-inset); }
.pt-key { font-size: 11px; color: var(--text-3); letter-spacing: 0.12em; font-weight: 700; }
.pt-name { font-size: 12px; color: var(--text-2); margin: 2px 0 8px; }
.pt-pace { font-size: 24px; }
.pt-to { font-size: 11px; color: var(--text-3); margin-top: 3px; font-family: var(--font-display); }
.pt-easy .pt-pace { color: var(--green); }
.pt-marathon .pt-pace { color: var(--blue); }
.pt-threshold .pt-pace { color: var(--cyan); }
.pt-interval .pt-pace { color: var(--orange); }
.pt-repetition .pt-pace { color: var(--red); }

.rings { display: flex; justify-content: space-around; padding: 6px 0 2px; }
.ring-foot { text-align: center; color: var(--text-3); font-size: 12px; margin-top: 10px; }
.rt-row { display: flex; gap: 12px; align-items: flex-start; }
.rt-icon { font-size: 24px; width: 44px; height: 44px; border-radius: 12px; background: rgba(200, 241, 105, 0.1);
  display: flex; align-items: center; justify-content: center; flex-shrink: 0; }

.recent-row { display: flex; align-items: center; padding: 8px 0; border-bottom: 1px dashed rgba(148,163,184,.12); font-size: 13px; }
.recent-meta { color: var(--text-3); font-size: 12px; margin-left: 8px; }
</style>
