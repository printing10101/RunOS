<template>
  <div>
    <div style="display:flex; align-items:flex-start; justify-content:space-between">
      <div>
        <h1 class="page-title">训练计划</h1>
        <div class="page-sub">目标驱动的周期化计划，一键下发到高驰 / 佳明手表</div>
      </div>
      <div>
        <el-button round @click="openAiGen" style="margin-right:10px">✦ AI 一句话生成</el-button>
        <el-button round style="margin-right:10px" @click="downloadIcs">导出日历 (.ics)</el-button>
        <el-button type="primary" round @click="showGen = true">生成新计划</el-button>
      </div>
    </div>

    <el-skeleton v-if="loading" style="margin-top:8px" :rows="8" animated />
    <template v-else-if="plan">
      <el-card shadow="never" style="margin-bottom:14px">
        <div class="plan-head">
          <div>
            <div class="plan-name">{{ plan.name }}</div>
            <div class="plan-meta">
              {{ plan.start_date }} → {{ plan.race_type === 'custom' ? '体验周截止' : '比赛日' }} {{ plan.race_date }} · 峰值周跑量 {{ plan.weekly_km_peak }} km
              <el-tag v-if="plan.target_time_str" size="small" effect="dark" class="target-chip">目标 {{ plan.target_time_str }}</el-tag>
            </div>
          </div>
          <div class="phase-legend">
            <span v-for="ph in phases" :key="ph.k"><i :style="{ background: ph.c }"></i>{{ ph.t }}</span>
          </div>
        </div>
        <el-alert v-if="plan.feasibility?.note" :type="plan.feasibility.feasible ? 'success' : 'warning'"
                  :title="plan.feasibility.note" style="margin-top:10px" :closable="false" show-icon />
        <el-alert v-if="plan.feasibility?.long_run_time && !plan.feasibility.long_run_time.ok"
                  type="warning" :title="plan.feasibility.long_run_time.note" style="margin-top:10px" :closable="false" show-icon />
        <el-alert v-if="plan.feasibility?.source === 'method_library'" type="info"
                  :title="`本计划为「${plan.feasibility.method_code}」方法体验周（VDOT ${plan.feasibility.vdot}），由知识库模板按你的真实数据自动降档生成`"
                  style="margin-top:10px" :closable="false" show-icon />
        <el-alert v-for="(n, i) in (plan.feasibility?.notes || [])" :key="'dn' + i"
                  type="info" :title="n" style="margin-top:8px" :closable="false" />
      </el-card>

      <el-card shadow="never">
        <div class="week-nav">
          <el-button size="small" circle @click="week = Math.max(0, week - 1)" :disabled="week === 0">‹</el-button>
          <b style="font-size:16px">第 {{ plan.weeks[week].week_index }} 周</b>
          <el-tag :color="phaseColor(plan.weeks[week].phase)" effect="dark" style="border: none; font-weight:700">
            {{ phaseName(plan.weeks[week].phase) }}
          </el-tag>
          <span class="week-note">{{ plan.weeks[week].phase_note }}</span>
          <span class="week-target">目标 {{ plan.weeks[week].target_km }} km</span>
          <el-button size="small" circle @click="week = Math.min(plan.weeks.length - 1, week + 1)" :disabled="week >= plan.weeks.length - 1">›</el-button>
        </div>

        <div class="week-grid">
          <div v-for="slot in weekSlots" :key="slot.weekday" class="day-col"
               :class="{ 'day-today': slot.isToday }">
            <div class="day-head">
              {{ WEEKDAY[slot.weekday] }}
              <span>{{ slot.date ? slot.date.slice(5).replace('-', '/') : '' }}</span>
              <em v-if="slot.isToday" class="day-today-tag">今天</em>
            </div>
            <template v-if="slot.workouts.length">
              <div v-for="wo in slot.workouts" :key="wo.id" class="wo-card"
                   :class="['wo-' + wo.session_type, { 'wo-done': wo.status === 'completed' }]"
                   :style="{ borderLeftColor: mindTag(wo).color }"
                   :title="wo.structured?.length ? '点击查看训练步骤' : ''" @click="openSteps(wo)">
                <div class="wo-tag" :style="{ background: mindTag(wo).color + '1f', color: mindTag(wo).color }">
                  {{ mindTag(wo).label }}
                </div>
                <div class="wo-title">{{ wo.title }}</div>
                <div class="wo-meta">{{ wo.distance_km ? wo.distance_km + 'km' : '' }} {{ wo.duration_min ? wo.duration_min + 'min' : '' }}</div>
                <div class="wo-tip" v-if="wo.diet_tip">🥗 {{ wo.diet_tip }}</div>
                <div class="wo-actions">
                  <el-button size="small" type="primary" round @click.stop="pushWo(wo)"
                             :loading="pushingId === wo.id" :disabled="!connectedAny">
                    {{ wo.pushed_platforms?.length ? '再次下发' : '下发手表' }}
                  </el-button>
                  <el-button size="small" round plain @click.stop="downloadFit(wo)">FIT</el-button>
                  <el-button size="small" round plain :type="wo.status === 'completed' ? 'success' : ''"
                             @click.stop="toggleDone(wo)">
                    {{ wo.status === 'completed' ? '✓' : '完成' }}
                  </el-button>
                </div>
                <el-tag v-if="wo.pushed_platforms?.length" size="small" type="success" effect="plain" style="margin-top:6px">
                  ✓ {{ wo.pushed_platforms.map(p => ({ garmin: '佳明', coros: '高驰' }[p])).join('/') }}
                </el-tag>
                <div v-if="wo.status === 'completed'" class="wo-comment" @click.stop>
                  <div v-if="analysisBadge(wo)" class="wc-analysis">
                    <el-tag :type="{ on_target: 'success', too_fast: 'warning', too_slow: 'warning', short: 'danger', no_data: 'info' }[wo.analysis?.verdict] || 'info'"
                            size="small" effect="plain">课况：{{ analysisBadge(wo) }}</el-tag>
                    <span v-if="wo.analysis?.reasons?.length" class="wc-analysis-reason">{{ wo.analysis.reasons[0] }}</span>
                  </div>
                  <div v-if="wo.coach_comment" class="wc-text" :title="wo.coach_comment">✦ {{ wo.coach_comment }}</div>
                  <div v-else-if="commentPending[wo.id]" class="wc-pending">✦ AI 点评生成中…</div>
                  <el-button v-else size="small" link type="primary" class="wc-btn"
                             :loading="commentLoading[wo.id]" @click="makeComment(wo)">✦ AI 点评</el-button>
                  <el-button v-if="wo.coach_comment" size="small" link class="wc-btn"
                             :loading="commentLoading[wo.id]" @click="makeComment(wo, true)">重新点评</el-button>
                </div>
              </div>
            </template>
            <div v-else class="rest-day">休息日</div>
          </div>
        </div>
      </el-card>

      <el-drawer v-model="showSteps" :title="stepsWo?.title" size="380">
        <div v-for="(s, i) in stepsWo?.structured || []" :key="i" class="step-row">
          <el-tag size="small" :type="stepTagType(s.step_type)">{{ stepName(s.step_type) }}</el-tag>
          <b style="margin-left:8px">{{ s.name }}</b>
          <span style="color:var(--text-3); margin-left:auto; font-size:13px">{{ stepDur(s) }}</span>
          <div style="color:var(--text-3); font-size:12px; margin-top:2px">{{ s.target?.label }} {{ s.note }}</div>
        </div>
      </el-drawer>
    </template>
    <el-empty v-else description="还没有训练计划，先设定目标并生成" style="margin-top:80px">
      <el-button type="primary" round @click="showGen = true">生成新计划</el-button>
    </el-empty>
    <el-drawer v-model="showGen" title="生成训练计划" size="430">
      <el-form label-width="90px">
        <el-form-item label="目标">
          <el-select v-model="genForm.goal_id" style="width:100%" placeholder="选择比赛目标">
            <el-option v-for="g in goals" :key="g.id" :value="g.id"
                       :label="`${g.target_label || g.race_type} ${g.target_date || ''}`" />
          </el-select>
          <div v-if="!goals.length" class="form-tip" style="margin-top:6px">
            还没有比赛目标，先到「<router-link to="/settings?tab=profile" style="color:var(--jade)">设置 → 个人档案</router-link>」添加一个目标
          </div>
        </el-form-item>
        <el-form-item label="开始日期">
          <el-date-picker v-model="genForm.start_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
        </el-form-item>
        <el-form-item label="峰值跑量">
          <el-input-number v-model="genForm.weekly_km_peak" :min="20" :max="160" style="width:100%" />
          <div class="form-tip">留空按当前水平自动推算；全马破三一般需要 70-90km</div>
        </el-form-item>
        <el-button type="primary" round style="width:100%" @click="generate" :loading="generating">生成周期化计划</el-button>
      </el-form>
    </el-drawer>

    <el-drawer v-model="showAiGen" title="AI 一句话生成计划" size="430">
      <div class="form-tip" style="margin-bottom:10px">
        用自然语言描述目标（本地 AI 解析，引擎算配速与可行性）：例如「10 月杭州马拉松跑进 330，周跑量巅峰 80」
      </div>
      <el-input v-model="aiText" type="textarea" :rows="3" :disabled="aiParsed"
                placeholder="例如：10月杭州马拉松跑进330" />
      <div style="display:flex; gap:10px; margin-top:12px">
        <el-button round :loading="aiParsing" :disabled="aiParsed || !aiText.trim()" @click="parseAi" style="flex:1">解析目标</el-button>
        <el-button round :disabled="!aiParsed" @click="resetAi">重填</el-button>
      </div>

      <template v-if="aiParsed">
        <el-divider />
        <div class="ai-parse-row"><span>项目</span><b>{{ { '800m': '800米', '1k': '1公里', '1500m': '1500米', '3k': '3公里', '5k': '5 公里', '10k': '10 公里', hm: '半程马拉松', marathon: '马拉松' }[aiParsed.race_type] }}</b></div>
        <div class="ai-parse-row"><span>标签</span><b>{{ aiParsed.target_label || '—' }}</b></div>
        <div class="ai-parse-row"><span>目标成绩</span><b>{{ targetStr }}</b></div>
        <div class="ai-parse-row"><span>比赛日期</span><b>{{ aiParsed.target_date || '未指定（默认 16 周）' }}</b></div>
        <div class="ai-parse-row"><span>当前 VDOT</span><b>{{ aiFeasibility?.current_vdot ?? '—' }}</b></div>
        <el-alert v-if="aiFeasibility?.note" :type="aiFeasibility.feasible ? 'success' : 'warning'"
                  :title="aiFeasibility.note" :closable="false" show-icon style="margin-top:12px" />
        <el-alert v-if="aiFeasibility?.long_run_time && !aiFeasibility.long_run_time.ok"
                  type="warning" :title="aiFeasibility.long_run_time.note" :closable="false" show-icon style="margin-top:10px" />
        <el-button type="primary" round style="width:100%; margin-top:16px" :loading="generating" @click="confirmAi">
          确认无误，生成周期化计划
        </el-button>
        <div class="form-tip" style="margin-top:8px">将创建新目标并生成计划，旧计划自动归档</div>
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, SESSION_STYLE } from '../api'
import { platName, phaseName, phaseColor, downloadFile, localDateStr, parseLocalDate, todayStr } from '../utils/common'

const plan = ref(null)
const goals = ref([])
const conn = ref({ connections: [], config_ready: {} })
const week = ref(0)
const showGen = ref(false)
const showSteps = ref(false)
const stepsWo = ref(null)
const generating = ref(false)
const loading = ref(true)
const pushingId = ref(null)
const genForm = ref({ goal_id: null, start_date: null, weekly_km_peak: null })

const WEEKDAY = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
const phases = [
  { k: 'base', t: '基础期', c: '#5fc987' }, { k: 'build', t: '强化期', c: '#5f9fc9' },
  { k: 'peak', t: '巅峰期', c: '#e05f5f' }, { k: 'taper', t: '减量期', c: '#d9a24e' },
]

const connectedAny = computed(() => conn.value.connections.some(c => c.status === 'connected'))

const today = todayStr()

const weekSlots = computed(() => {
  const out = Array.from({ length: 7 }, (_, wd) => ({ weekday: wd, date: '', isToday: false, workouts: [] }))
  const wk = plan.value?.weeks?.[week.value]
  if (!wk) return out
  // 以周的 start_date（后端已归一为周一）补齐整周 7 天的日期：
  // 只依赖课次日期的话，没排课的星期会显示不出日期，用户无法确认这周到底是哪几天
  if (wk.start_date) {
    const base = parseLocalDate(wk.start_date)
    for (let wd = 0; wd < 7; wd++) {
      const d = new Date(base.getFullYear(), base.getMonth(), base.getDate() + wd)
      out[wd].date = localDateStr(d)
    }
  }
  for (const wo of wk.workouts) {
    const wd = (parseLocalDate(wo.date).getDay() + 6) % 7
    if (!out[wd].date) out[wd].date = wo.date
    out[wd].workouts.push(wo)
  }
  // 标出「今天」，让用户一眼定位当前处在计划的哪一天
  for (const slot of out) slot.isToday = slot.date === today
  return out
})

function stepName(t) { return { warmup: '热身', active: '主训练', rest: '恢复', cooldown: '冷身', strength: '力量' }[t] || t }
function stepTagType(t) { return { warmup: 'info', active: 'danger', rest: 'warning', cooldown: 'info', strength: '' }[t] || '' }
function stepDur(s) {
  if (s.duration_type === 'time') return `${s.duration_value} 分钟`
  if (s.duration_type === 'distance') return s.duration_value >= 1000 ? `${s.duration_value / 1000} km` : `${s.duration_value} m`
  return '开放时长'
}

// 课型心智标签：一眼看懂每步课的类型；quality 课再按标题细分（节奏/间歇/马配）
function mindTag(wo) {
  const t = wo.session_type
  if (t === 'quality') {
    const s = wo.title || ''
    if (s.includes('间歇')) return { label: '间歇跑', color: '#e05f5f' }
    if (s.includes('巡航') || s.includes('节奏')) return { label: '节奏跑', color: '#d98f4a' }
    if (s.includes('马配') || s.includes('马拉松')) return { label: '马拉松配速', color: '#e05f5f' }
    if (s.includes('激活')) return { label: '赛前激活', color: '#5f9fc9' }
    return { label: '质量课', color: '#e05f5f' }
  }
  const base = SESSION_STYLE[t]
  return base ? { label: base.label, color: base.color } : { label: t, color: '#7d938c' }
}

async function load() {
  loading.value = true
  try {
    // 计划依赖 week 联动须先到；档案与连接互相独立，与计划并行拉取省 1-2 个 RTT
    const [ath, connData, planData] = await Promise.all([
      api.get('/athlete'), api.get('/connections'), api.get('/plan/current'),
    ])
    plan.value = planData
    if (plan.value) week.value = Math.min(week.value, plan.value.weeks.length - 1)
    goals.value = ath.goals.filter(g => g.status === 'active')
    if (!genForm.value.goal_id && goals.value.length) genForm.value.goal_id = goals.value[0].id
    conn.value = connData
  } finally {
    loading.value = false
  }
}

async function generate() {
  generating.value = true
  try {
    await api.post('/plan/generate', genForm.value)
    ElMessage.success('计划已生成')
    showGen.value = false
    week.value = 0
    await load()
  } catch (e) {
    ElMessage.error(e.message || '生成失败')
  } finally { generating.value = false }
}

// ---- AI 一句话生成 ----
const showAiGen = ref(false)
const aiText = ref('')
const aiParsing = ref(false)
const aiParsed = ref(null)      // {race_type, target_time_sec, target_date, target_label, weekly_km_peak}
const aiFeasibility = ref(null)
const targetStr = computed(() => {
  const s = aiParsed.value?.target_time_sec
  if (!s) return '无成绩目标'
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}` : `${m}:${String(sec).padStart(2, '0')}`
})

function openAiGen() { showAiGen.value = true }

function resetAi() { aiParsed.value = null; aiFeasibility.value = null; aiText.value = '' }

async function parseAi() {
  aiParsing.value = true
  try {
    const r = await fetch('/api/ai/plan/from-text', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: aiText.value.trim() }),
    })
    const d = await r.json()
    if (!r.ok) throw new Error(d.detail || '解析失败')
    aiParsed.value = d.params
    aiFeasibility.value = { ...d.feasibility, current_vdot: d.current_vdot }
  } catch (e) {
    ElMessage.error(e.message || '本地模型不可用，请重启平台自动拉起本地模型服务（llama-server），详见 AI 教练页')
  } finally { aiParsing.value = false }
}

async function confirmAi() {
  generating.value = true
  try {
    await api.post('/ai/plan/confirm', aiParsed.value)
    ElMessage.success('计划已生成')
    showAiGen.value = false
    resetAi()
    week.value = 0
    await load()
  } catch (e) {
    ElMessage.error(e.message || '生成失败')
  } finally { generating.value = false }
}

async function pushWo(wo) {
  // 与后端能力一致：只发支持写入的平台；已成功过的平台由后端按 pushed_platforms 幂等跳过
  const opts = ['garmin', 'strava'].filter(p => conn.value.connections.some(c => c.platform === p && c.status === 'connected'))
  if (!opts.length) { ElMessage.warning('请先在「平台连接」页绑定佳明 / Strava（高驰暂未开放训练写入，可下载 FIT 手动导入）'); return }
  pushingId.value = wo.id
  try {
    const r = await api.post(`/plan/workouts/${wo.id}/push`, { platforms: opts })
    if (!r.pushed.length) {
      ElMessage.error(r.results?.filter(x => !x.ok).map(x => `${platName(x.platform)}：${x.error}`).join('；') || '下发失败')
    } else {
      ElMessage.success(`已下发：${r.pushed.map(platName).join('、')}，打开对应 App 同步到手表即可`)
    }
    await load()
  } catch (e) {
    ElMessage.error(e.message || '下发失败')
  } finally { pushingId.value = null }
}

function openSteps(wo) {
  if (!wo.structured?.length) { ElMessage.info('该课没有结构化步骤'); return }
  stepsWo.value = wo
  showSteps.value = true
}

// ---- 单课分析（处方 vs 实际）的展示口径 ----
const ANALYSIS_LABELS = { on_target: '达标', too_fast: '快于处方', too_slow: '慢于处方', short: '未跑完', no_data: '无实际数据' }
function analysisBadge(wo) {
  return wo.analysis ? (ANALYSIS_LABELS[wo.analysis.verdict] || wo.analysis.verdict) : ''
}

async function toggleDone(wo) {
  const done = wo.status !== 'completed'
  try {
    const r = await api.post(`/plan/workouts/${wo.id}/complete`, { completed: done })
    wo.status = done ? 'completed' : 'planned'
    if (r.analysis) wo.analysis = r.analysis
    ElMessage.success(done ? '已标记完成，AI 点评正在后台生成' : '已取消完成')
    if (done) scheduleCommentPoll(wo.id)
    else commentPending.value[wo.id] = false
  } catch (e) {
    ElMessage.error(e.message || '操作失败')
  }
}

// ---- 训练后 AI 点评 ----
const commentPending = ref({})   // 完成打卡后等待后台生成
const commentLoading = ref({})   // 手动生成/重新生成请求进行中
const commentTimers = {}

async function makeComment(wo, force = false) {
  commentLoading.value[wo.id] = true
  try {
    const r = await api.post('/ai/workout-comment', { workout_id: wo.id, force })
    wo.coach_comment = r.comment
    wo.coach_comment_at = r.at
    commentPending.value[wo.id] = false
    if (!force) ElMessage.success(r.source === 'rule' ? '已生成规则点评（本地模型未就绪）' : 'AI 点评已生成')
  } catch (e) {
    ElMessage.error(e.message || '点评生成失败')
  } finally { commentLoading.value[wo.id] = false }
}

// 打卡后点评由后端后台线程生成：分两次延迟刷新拿结果，超时未生成则回落到手动按钮
function scheduleCommentPoll(workoutId) {
  commentPending.value[workoutId] = true
  clearTimeout(commentTimers[workoutId])
  clearTimeout(commentTimers[workoutId + ':2'])
  clearTimeout(commentTimers[workoutId + ':expire'])
  commentTimers[workoutId] = setTimeout(() => load(), 12000)
  commentTimers[workoutId + ':2'] = setTimeout(() => load(), 30000)
  commentTimers[workoutId + ':expire'] = setTimeout(() => { commentPending.value[workoutId] = false }, 60000)
}

onUnmounted(() => { Object.values(commentTimers).forEach(clearTimeout) })

async function downloadFit(wo) {
  await downloadFile(`/api/plan/workouts/${wo.id}/fit`, `workout_${wo.date}.fit`,
    { successTip: '已导出 FIT 文件，可在高驰/佳明 App 中导入' })
}

async function downloadIcs() {
  await downloadFile('/api/plan/calendar.ics', 'training_plan.ics',
    { successTip: '已导出日历文件，可导入 Apple/Google/Outlook 日历', failTip: '暂无有效训练计划' })
}

onMounted(load)
</script>

<style scoped>
.plan-name { font-size: 17px; font-weight: 800; }
.plan-meta { color: var(--text-3); font-size: 13px; margin-top: 5px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.target-chip { background: rgba(63, 208, 164, 0.14); color: var(--jade); border: 1px solid rgba(63, 208, 164, 0.3); font-weight: 700; }
.plan-head { display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 10px; }
.phase-legend { display: flex; gap: 14px; font-size: 12px; color: var(--text-2); }
.phase-legend i { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 4px; }

.week-nav { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
.week-note { color: var(--text-3); font-size: 13px; flex: 1; }
.week-target { color: var(--text-2); font-size: 13px; }

.week-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 8px; }
.day-head { font-size: 12px; color: var(--text-3); margin-bottom: 6px; display: flex; align-items: center; gap: 5px; }
.day-head span { margin-left: auto; }
/* 「今天」标记：让用户一眼定位当前处在计划的哪一天 */
.day-today-tag { font-style: normal; font-size: 10px; font-weight: 700; color: var(--jade);
  background: rgba(63, 208, 164, 0.14); border: 1px solid rgba(63, 208, 164, 0.3);
  border-radius: 5px; padding: 0 4px; line-height: 15px; }
.day-col.day-today { background: rgba(63, 208, 164, 0.06); border-radius: 10px;
  box-shadow: inset 0 0 0 1px rgba(63, 208, 164, 0.25); }
.day-col.day-today .day-head { color: var(--text-1); font-weight: 700; }
.wo-card {
  border: 1px solid var(--border); border-left: 3px solid var(--text-3); border-radius: 10px;
  padding: 9px; background: var(--bg-inset); cursor: pointer;
}
.wo-done { opacity: .78; }
.wo-easy { border-left-color: #5fc987; }
.wo-quality { border-left-color: #e05f5f; }
.wo-long { border-left-color: #d9a24e; }
.wo-strength { border-left-color: #5f9fc9; }
.wo-title { font-size: 13px; font-weight: 700; line-height: 1.35; }
.wo-tag { display: inline-block; font-size: 10.5px; font-weight: 700; padding: 1px 7px; border-radius: 8px; margin-bottom: 4px; letter-spacing: .02em; }
.wo-meta { font-size: 12px; color: var(--text-3); margin: 4px 0; font-family: var(--font-display); letter-spacing: .04em; }
.wo-tip { font-size: 11px; color: var(--orange); background: rgba(217, 162, 78, 0.08); border-radius: 5px; padding: 2px 6px; margin-bottom: 7px; }
.wo-comment { margin-top: 6px; }
.wc-analysis { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; flex-wrap: wrap; }
.wc-analysis-reason { font-size: 11.5px; color: var(--text-3, #9ab0a7); }
.wc-text {
  font-size: 11px; color: var(--text-2); line-height: 1.55;
  background: rgba(63, 208, 164, 0.06); border: 1px dashed rgba(63, 208, 164, 0.25);
  border-radius: 6px; padding: 4px 7px; cursor: default;
  display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden;
}
.wc-pending { font-size: 11px; color: var(--text-3); }
.wc-btn { padding: 0; font-size: 11px; height: auto; margin-right: 8px; }
.wo-actions { display: flex; flex-wrap: wrap; gap: 4px; }
.wo-actions .el-button { padding: 4px 10px; margin-left: 0; }
.rest-day {
  color: var(--text-3); font-size: 12px; text-align: center; padding: 18px 0;
  border: 1px dashed rgba(157, 184, 173, 0.2); border-radius: 10px;
}
.step-row { padding: 10px 0; border-bottom: 1px dashed rgba(157, 184, 173, 0.12); }
.form-tip { font-size: 11px; color: var(--text-3); line-height: 1.4; }
.ai-parse-row { display: flex; justify-content: space-between; padding: 7px 0; font-size: 13.5px; color: var(--text-2); border-bottom: 1px dashed rgba(157, 184, 173, 0.1); }
</style>
