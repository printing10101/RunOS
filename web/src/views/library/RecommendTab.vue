<template>
  <div>
    <el-alert type="info" :closable="false" style="margin-bottom:14px"
      title="推荐完全基于你的真实训练数据（近 28 天周跑量 / 可练天数 / 目标 / 伤病信号），每条推荐都给出命中理由与证据等级。" />

    <div v-if="loading" v-loading="loading" style="min-height:200px"></div>

    <template v-else-if="data && data.ok">
      <!-- 画像摘要 -->
      <el-card shadow="never" class="profile-card">
        <div class="profile-row">
          <div class="p-item"><span class="p-label">周跑量</span><b>{{ data.profile.weekly_km }} km</b></div>
          <div class="p-item"><span class="p-label">水平档位</span><b>{{ levelLabel(data.profile.level) }}</b></div>
          <div class="p-item"><span class="p-label">每周可练</span><b>{{ data.profile.days_per_week }} 天</b></div>
          <div class="p-item"><span class="p-label">当前目标</span><b>
            <template v-if="goalText">{{ goalText }}<span v-if="data.profile.goal_source === 'plan'"
              class="goal-src">来自课表</span></template>
            <template v-else>未设定</template>
          </b></div>
          <div class="p-item"><span class="p-label">周期阶段</span><b>{{ data.profile.phase ? phaseName(data.profile.phase) : '—' }}</b></div>
          <div class="p-item"><span class="p-label">伤病信号</span><b :class="{ 'p-warn': data.profile.injury_sensitive }">{{ data.profile.injury_sensitive ? '有' : '无' }}</b></div>
        </div>
      </el-card>

      <!-- 方法推荐 -->
      <h3 class="sec-title">为你推荐的训练方法</h3>
      <el-row :gutter="14">
        <el-col :span="12" v-for="it in data.items" :key="it.method.code" style="margin-bottom:14px">
          <el-card shadow="never" class="rec-card">
            <div class="rec-head">
              <div>
                <b class="rec-name">{{ it.method.name_zh }}</b>
                <span class="rec-ja" v-if="it.method.name_ja">{{ it.method.name_ja }}</span>
              </div>
              <div class="rec-score">匹配 <RollNum :value="it.score" /></div>
            </div>
            <div class="rec-tags">
              <el-tag size="small" :type="evTagType(it.method.evidence_level)" effect="dark">证据 {{ it.method.evidence_level }}</el-tag>
              <el-tag size="small" :type="it.method.asian_fit >= 80 ? 'success' : 'info'">亚洲适配 {{ it.method.asian_fit }}</el-tag>
              <el-tag v-for="t in (it.method.tags || []).slice(0, 3)" :key="t" size="small" effect="plain">{{ t }}</el-tag>
            </div>
            <ul class="rec-reasons">
              <li v-for="(r, i) in it.reasons" :key="i">{{ r }}</li>
            </ul>
            <div class="rec-summary">{{ it.method.summary }}</div>
            <div class="rec-actions">
              <el-tag v-if="currentMethodCode === it.method.code" size="small" type="success" effect="dark">当前使用中</el-tag>
              <el-button size="small" type="primary" plain round
                         :disabled="currentMethodCode === it.method.code"
                         :loading="applyingCode === it.method.code"
                         @click="applyWeek(it.method)">选用该理念的课表</el-button>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 计划匹配 -->
      <h3 class="sec-title">匹配的参考计划骨架</h3>
      <el-empty v-if="!plans?.items?.length" description="暂无匹配的参考计划，建议先积累跑量后重试" />
      <el-row :gutter="14" v-else>
        <el-col :span="12" v-for="it in plans.items" :key="it.plan.code" style="margin-bottom:14px">
          <el-card shadow="never" class="rec-card">
            <div class="rec-head">
              <div>
                <b class="rec-name">{{ it.plan.name_zh }}</b>
                <span class="rec-ja">{{ it.plan.author }}</span>
              </div>
              <div class="rec-score">匹配 <RollNum :value="it.score" /></div>
            </div>
            <div class="rec-tags">
              <el-tag size="small" effect="plain">{{ it.plan.weeks }} 周</el-tag>
              <el-tag size="small" effect="plain">峰值 {{ it.plan.weekly_km_range?.min }}-{{ it.plan.weekly_km_range?.max }} km/周</el-tag>
              <el-tag size="small" effect="plain">每周 {{ it.plan.days_per_week?.min }}-{{ it.plan.days_per_week?.max }} 练</el-tag>
              <el-tag size="small" :type="evTagType(it.plan.evidence_level)" effect="dark">证据 {{ it.plan.evidence_level }}</el-tag>
            </div>
            <ul class="rec-reasons">
              <li v-for="(r, i) in it.reasons" :key="i">{{ r }}</li>
            </ul>
            <el-collapse class="plan-detail">
              <el-collapse-item :title="'计划骨架（' + it.plan.key_workouts?.length + ' 项关键课）'">
                <div class="plan-block" v-if="weekTemplateText(it.plan).length">
                  <div class="pb-title">周模板</div>
                  <div class="pb-body">{{ weekTemplateText(it.plan).join(' · ') }}</div>
                </div>
                <div class="plan-block" v-if="longRunText(it.plan)">
                  <div class="pb-title">长距离递进</div>
                  <div class="pb-body">{{ longRunText(it.plan) }}</div>
                </div>
                <div class="plan-block">
                  <div class="pb-title">关键课</div>
                  <ul class="pb-list"><li v-for="(k, i) in it.plan.key_workouts" :key="i">{{ k }}</li></ul>
                </div>
                <div class="plan-block" v-if="it.plan.taper_plan">
                  <div class="pb-title">减量方案</div>
                  <div class="pb-body">{{ it.plan.taper_plan }}</div>
                </div>
                <div class="plan-block">
                  <div class="pb-title">注意事项</div>
                  <div class="pb-body warn">{{ it.plan.cautions }}</div>
                </div>
                <div class="plan-block" v-if="it.plan.sources?.length">
                  <div class="pb-title">出处</div>
                  <div class="pb-src" v-for="(s, i) in it.plan.sources" :key="i">
                    <el-tag size="small" :type="evTagType(s.level)" effect="dark">{{ s.level }}</el-tag>
                    <a v-if="s.url" :href="s.url" target="_blank" rel="noopener">{{ s.title }}</a>
                    <span v-else>{{ s.title }}</span>
                  </div>
                </div>
              </el-collapse-item>
            </el-collapse>
          </el-card>
        </el-col>
      </el-row>

      <div class="foot-note" v-if="plans?.traceability_note">ℹ️ {{ plans.traceability_note }}</div>
    </template>

    <el-empty v-else :description="data?.reasons?.[0] || '暂无足够训练数据，先去同步/录入跑步记录吧'" />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../../api'
import { phaseName } from '../../utils/common'
import RollNum from '../../components/RollNum.vue'

const router = useRouter()
const loading = ref(true)
const data = ref(null)     // 方法推荐（含画像）
const plans = ref(null)    // 计划匹配
const applyingCode = ref(null)      // 正在生成体验周的方法 code
const currentMethodCode = ref(null) // 当前 active 计划若由某方法生成，记其 code

const LEVEL = { beginner: '入门', intermediate: '进阶', advanced: '高级', elite: '精英' }
const RACE = { '5k': '5K', '10k': '10K', hm: '半马', marathon: '全马' }
const WK = { mon: '一', tue: '二', wed: '三', thu: '四', fri: '五', sat: '六', sun: '日' }
const SESSION = {
  easy: '轻松跑', easy_short: '短轻松跑', recovery: '恢复跑', recovery_cross: '恢复/交叉',
  tempo: '节奏跑', interval: '间歇', long: '长距离', long_pace: '带配速长距离', long_with_mp: '马配长距离',
  medium_long: '中长距离', medium_long_or_LT: '中长距离/阈值', LT_or_VO2max: '阈值/VO2max',
  general_aerobic: '一般有氧', speed_sos: '速度 SOS', tempo_or_mp_sos: '节奏/马配 SOS',
  track_intervals: '跑道间歇', threshold_am: '阈值课(早)', threshold_pm: '阈值课(晚)',
  quality_1: '质量课 Q1', quality_2: '质量课 Q2', strength: '力量', cross: '交叉训练',
  race: '比赛', rest: '休息',
}

const levelLabel = v => LEVEL[v] || v
// 目标口径与后端 build_athlete_profile 对齐：无手动目标时回退课表指向的比赛；
// custom（高驰镜像）计划没有标准项目名，只显示排期日期，避免漏出原文 custom
const goalText = computed(() => {
  const p = data.value?.profile
  if (!p?.race_type) return ''
  const name = p.race_type === 'custom' ? '课表排期' : (RACE[p.race_type] || p.race_type)
  return p.race_date ? `${name} · ${String(p.race_date).slice(5)}` : name
})
const evTagType = lv => ({ A: 'success', B: 'primary', C: 'warning', D: 'info' }[lv] || 'info')

function weekTemplateText(plan) {
  const wt = plan.week_template || {}
  return Object.entries(wt)
    .filter(([k, v]) => WK[k] && SESSION[v])
    .map(([k, v]) => `周${WK[k]} ${SESSION[v]}`)
}
function longRunText(plan) {
  const lr = plan.long_run_progression || []
  return lr.map(x => `W${x.week}:${x.km}km${x.note ? `(${x.note})` : ''}`).join(' → ')
}

// 与 MethodsTab 的体验周入口同一端点：生成一周体验课表并替换当前计划（旧计划归档）
async function applyWeek(method) {
  const ok = await ElMessageBox.confirm(
    `将按你的真实数据（周跑量 / 可练时段 / 当前 VDOT）生成「${method.name_zh}」的一周体验课表，`
    + '并设为当前训练计划（原有进行中计划会被归档，课表从下一个周一开始）。继续？',
    '选用该理念课表', { type: 'warning', confirmButtonText: '生成', cancelButtonText: '取消' }).catch(() => false)
  if (!ok) return
  applyingCode.value = method.code
  try {
    const r = await api.post(`/methods/${method.code}/apply-week`)
    currentMethodCode.value = method.code
    ElMessage.success(r.message || '体验周已生成')
    router.push('/plan')
  } catch (e) {
    ElMessage.error(e.message || '生成失败')
  } finally { applyingCode.value = null }
}

onMounted(async () => {
  try {
    const [a, p, cur] = await Promise.all([
      api.get('/methods/recommend'),
      api.get('/methods/plans/recommend').catch(() => null),
      api.get('/plan/current').catch(() => null),
    ])
    data.value = a
    plans.value = p
    if (cur?.feasibility?.source === 'method_library') currentMethodCode.value = cur.feasibility.method_code
  } finally { loading.value = false }
})
</script>

<style scoped>
.profile-card { margin-bottom: 18px; }
.profile-row { display: flex; gap: 28px; flex-wrap: wrap; }
.p-item { display: flex; flex-direction: column; gap: 2px; }
.p-label { font-size: 12px; color: var(--text-3); }
.p-warn { color: var(--orange); }
/* 「来自课表」来源小徽标：区分手动设定的目标与课表回退的事实目标 */
.goal-src { font-size: 10px; font-weight: 400; color: var(--text-3); border: 1px solid var(--border);
  border-radius: 8px; padding: 0 6px; margin-left: 6px; vertical-align: 1px; }
.sec-title { margin: 6px 0 12px; font-size: 15px; }
.rec-card { height: 100%; }
.rec-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
.rec-name { font-size: 15px; }
.rec-ja { display: block; font-size: 11px; color: var(--text-3); margin-top: 2px; }
.rec-score { flex-shrink: 0; font-size: 20px; color: var(--lime); font-family: var(--font-display); }
.rec-tags { margin: 8px 0; display: flex; gap: 6px; flex-wrap: wrap; }
.rec-reasons { margin: 6px 0 8px; padding-left: 18px; color: var(--text-2); font-size: 12px; line-height: 1.8; }
.rec-reasons li { margin: 0; }
.rec-summary { color: var(--text-3); font-size: 12px; line-height: 1.7;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.rec-actions { margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--border);
  display: flex; justify-content: flex-end; align-items: center; gap: 8px; }
.plan-detail { border-top: 1px dashed var(--border); }
.plan-block { margin-bottom: 10px; }
.pb-title { font-size: 11px; color: var(--text-3); margin-bottom: 4px; }
.pb-body { font-size: 12px; line-height: 1.8; color: var(--text-2); }
.pb-body.warn { color: var(--orange); }
.pb-list { margin: 0; padding-left: 18px; font-size: 12px; line-height: 1.8; color: var(--text-2); }
.pb-src { display: flex; align-items: center; gap: 6px; font-size: 12px; margin-top: 4px; }
.pb-src a { color: var(--blue); text-decoration: none; }
.foot-note { color: var(--text-3); font-size: 12px; margin-top: 8px; }
</style>
