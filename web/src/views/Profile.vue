<template>
  <div>
    <el-row :gutter="14">
      <el-col :span="14">
        <el-card shadow="never">
          <template #header>
            <span>跑者档案</span>
            <span class="hdr-tip">
              标「实测 / 推导 / 估算」的字段会随你的训练与体测数据自动更新
            </span>
          </template>
          <el-form ref="formRef" :model="form" :rules="rules" label-width="110px">
            <el-form-item label="姓名" prop="name">
              <el-input v-model="form.name" maxlength="20" placeholder="你的称呼" />
            </el-form-item>
            <el-form-item label="性别" prop="sex">
              <el-radio-group v-model="form.sex">
                <el-radio value="male">男</el-radio>
                <el-radio value="female">女</el-radio>
              </el-radio-group>
              <div class="field-tip">性别与出生年份是一切生理推算的输入，必须由你提供</div>
            </el-form-item>
            <el-form-item label="出生年份" prop="birth_year">
              <el-input-number v-model="form.birth_year" :min="1930" :max="2015" style="width:100%" />
              <div class="field-tip">用年龄推算最大心率、同年龄百分位等，必须由你提供</div>
            </el-form-item>
            <el-form-item label="身高 (cm)" prop="height_cm">
              <el-input-number v-model="form.height_cm" :min="120" :max="230" style="width:100%" />
              <div class="field-tip">用于 BMI 与身体条件评估，无法从训练数据推导</div>
            </el-form-item>

            <el-form-item v-for="f in AUTO_FIELDS" :key="f.key" :label="f.label" :prop="f.key">
              <el-input-number v-model="form[f.key]" :min="f.min" :max="f.max"
                               :precision="f.precision" :disabled="!isAuto(f.key)"
                               style="width:100%" />
              <div class="field-tip">
                <span v-if="srcOf(f.key)" class="src-badge" :class="'src-' + srcOf(f.key).source">
                  {{ srcOf(f.key).source_label }}
                </span>{{ srcOf(f.key)?.basis }}
              </div>
              <div class="auto-row">
                <el-switch size="small" :model-value="isAuto(f.key)"
                           @change="v => toggleAuto(f.key, v)" />
                <span class="auto-label">自动推算</span>
                <span v-if="isAuto(f.key)" class="auto-note">跟随你的训练/体测数据滚动更新</span>
                <span v-else class="auto-note">已钉死为你填写的值，不再自动变化</span>
              </div>
            </el-form-item>

            <el-alert v-if="pending.length" type="info" :closable="false" show-icon
                      :title="'还缺这些数据，补上后相关评估会更准：' + pendingText" />
            <el-button type="primary" round style="width:100%; margin-top:12px"
                       :loading="saving" @click="save">
              {{ form.id ? '保存修改' : '创建档案' }}
            </el-button>
          </el-form>
        </el-card>
      </el-col>

      <el-col :span="10">
        <el-card shadow="never">
          <template #header>比赛目标</template>
          <el-empty v-if="!goals.length" description="还没有目标" :image-size="60" />
          <div v-for="g in goals" :key="g.id" class="goal-row">
            <div class="goal-main">
              <div class="goal-title-line">
                <b>{{ g.target_label || g.race_type }}</b>
                <span v-if="g.achieved" class="goal-badge done">🎉 已达成</span>
                <span v-else-if="g.progress != null" class="goal-badge">{{ Math.round(g.progress * 100) }}%</span>
                <span v-else-if="g.status === 'paused'" class="goal-badge paused">已搁置</span>
              </div>
              <div class="goal-meta">
                {{ { '5k': '5公里', '10k': '10公里', hm: '半马', marathon: '全马' }[g.race_type] }}
                <template v-if="g.target_time_sec"> · 目标 {{ fmtTime(g.target_time_sec) }}</template>
                <template v-if="g.target_date"> · {{ g.target_date }}</template>
                <template v-if="g.progress == null && g.status === 'active'"> · 暂无预测，先攒数据</template>
              </div>
            </div>
            <div class="goal-right">
              <svg v-if="g.progress != null" viewBox="0 0 36 36" class="goal-ring" :class="{ done: g.achieved }">
                <circle cx="18" cy="18" r="15.8" fill="none" class="ring-track" />
                <circle cx="18" cy="18" r="15.8" fill="none" class="ring-fill"
                        :stroke-dasharray="`${Math.max(0.1, g.progress * 99.3)} 99.3`" />
              </svg>
              <el-button size="small" type="danger" plain round @click="delGoal(g.id)">删除</el-button>
            </div>
          </div>
          <el-divider />
          <el-form label-width="86px">
            <el-form-item label="项目">
              <el-select v-model="goalForm.race_type" style="width:100%">
                <el-option value="5k" label="5 公里" />
                <el-option value="10k" label="10 公里" />
                <el-option value="hm" label="半程马拉松" />
                <el-option value="marathon" label="全程马拉松" />
              </el-select>
            </el-form-item>
            <el-form-item label="目标成绩">
              <el-input v-model="goalForm.time_text" placeholder="如 3:30:00 / 22:30，可不填" />
            </el-form-item>
            <el-form-item label="比赛日期">
              <el-date-picker v-model="goalForm.target_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
            </el-form-item>
            <el-button round style="width:100%" :disabled="!form.id" @click="addGoal">添加目标</el-button>
            <div v-if="!form.id" class="field-tip" style="margin-top:8px">先创建档案后才能添加目标</div>
          </el-form>
        </el-card>

        <el-card shadow="never" style="margin-top:14px">
          <template #header>数据是怎么来的</template>
          <div class="legend">
            <div class="lg-row"><span class="src-badge src-measured">实测</span>你设备/体测直接量到的，优先级最高</div>
            <div class="lg-row"><span class="src-badge src-derived">按你的数据推导</span>用你别的属性按生理公式算的</div>
            <div class="lg-row"><span class="src-badge src-estimated">按你的属性估算</span>还没有实测数据，按年龄/性别/训练年限估的</div>
            <div class="lg-row"><span class="src-badge src-user">你填写的</span>你手动填的值，不会被估算值覆盖</div>
            <div class="lg-row"><span class="src-badge src-unknown">数据不足</span>没有依据，系统不会替你编一个数</div>
          </div>
          <ol class="next-steps">
            <li>绑定高驰 / 佳明同步训练与体测数据（心率、静息心率、体重）；</li>
            <li>数据进来后，静息心率/最大心率/训练年限等会自动跟着更新；</li>
            <li>想让某个字段固定不动，把它的「自动推算」开关关掉即可。</li>
          </ol>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { api, fmtTime } from '../api'

const formRef = ref()
const saving = ref(false)
const form = reactive({ id: null, name: '', sex: '', birth_year: null, height_cm: null,
                        weight_kg: null, resting_hr: null, max_hr: null, hrv_baseline: null,
                        training_age_years: null })
const goals = ref([])
const goalForm = reactive({ race_type: 'marathon', time_text: '', target_date: null })

/** 可自动推算的字段（与后端 services/profile.AUTO_FIELDS 一致）。 */
const AUTO_FIELDS = [
  { key: 'weight_kg', label: '体重 (kg)', min: 30, max: 200, precision: 1 },
  { key: 'resting_hr', label: '静息心率', min: 30, max: 110, precision: 0 },
  { key: 'max_hr', label: '最大心率', min: 100, max: 230, precision: 0 },
  { key: 'hrv_baseline', label: 'HRV 基线', min: 10, max: 200, precision: 0 },
  { key: 'training_age_years', label: '系统训练年限', min: 0, max: 80, precision: 1 },
]

/** 后端解析出的画像：{fields: {key: {value, source, source_label, basis, locked}}} */
const profile = ref({ fields: {}, pending: [] })
const FIELD_LABELS = { weight_kg: '体重', hrv_baseline: 'HRV 基线', height_cm: '身高', sex: '性别', age: '出生年份' }
const pending = computed(() => profile.value.pending || [])
const pendingText = computed(() => pending.value.map(k => FIELD_LABELS[k] || k).join('、'))

const srcOf = (key) => profile.value.fields?.[key] || null
const isAuto = (key) => {
  const f = srcOf(key)
  // 没有元数据（首次建档 / 老数据）默认按「自动」呈现；显式 locked 才算手动
  return !(f && f.locked)
}

// 必填项只保留「无法从数据推导的身份事实」；可解析字段留空交给系统
const rules = {
  name: [{ required: true, message: '请填写姓名', trigger: 'blur' }],
  sex: [{ required: true, message: '请选择性别', trigger: 'change' }],
  birth_year: [{ required: true, message: '请填写出生年份', trigger: 'change' }],
  height_cm: [{ required: true, message: '请填写身高', trigger: 'change' }],
  weight_kg: [{ required: true, message: '请填写体重', trigger: 'change' }],
}

onMounted(load)
async function load() {
  const d = await api.get('/athlete')
  Object.keys(form).forEach(k => { form[k] = null })
  if (d.athlete) {
    form.id = d.athlete.id
    Object.assign(form, d.athlete)
  }
  profile.value = d.profile || { fields: {}, pending: [] }
  goals.value = d.goals || []
  if (d.profile?.changed?.length) {
    ElMessage.success('已按最新数据自动更新：' +
      d.profile.changed.map(k => `${FIELD_LABELS[k] || k} → ${d.athlete[k]}`).join('、'))
  }
}

async function save() {
  await formRef.value.validate()
  saving.value = true
  try {
    const body = {}
    // 只提交有值的字段：留空 = 保持现状/交给系统自动解析
    Object.keys(form).forEach(k => { if (form[k] !== null && form[k] !== '') body[k] = form[k] })
    delete body.id
    await api.put('/athlete', body)
    ElMessage.success(form.id ? '档案已更新' : '档案已创建')
    window.dispatchEvent(new CustomEvent('athlete-updated'))
    await load()
  } catch (e) {
    ElMessage.error(e.message || '保存失败')
  } finally { saving.value = false }
}

async function toggleAuto(key, auto) {
  if (!form.id) { ElMessage.warning('先创建档案'); return }
  try {
    const d = await api.put('/athlete/auto', { field: key, auto })
    profile.value = d.profile || profile.value
    const f = srcOf(key)
    if (f) form[key] = f.value
    ElMessage.success(auto ? `${key} 已交给系统自动推算` : `${key} 已钉死为当前值`)
  } catch (e) { ElMessage.error(e.message || '切换失败') }
}

function parseTimeSec(text) {
  const t = (text || '').trim()
  if (!t) return null
  const parts = t.split(':').map(x => parseInt(x, 10))
  if (parts.some(isNaN) || !parts.length) { ElMessage.error('成绩格式：分:秒 或 时:分:秒'); return null }
  return parts.reduce((acc, v) => acc * 60 + v, 0)
}

const savingGoal = ref(false)
async function addGoal() {
  const target_time_sec = parseTimeSec(goalForm.time_text)
  if (goalForm.time_text && target_time_sec === null) return
  if (savingGoal.value) return
  savingGoal.value = true
  try {
    await api.post('/athlete/goals', {
      race_type: goalForm.race_type, target_time_sec,
      target_date: goalForm.target_date, target_label: '',
    })
    goalForm.time_text = ''
    goalForm.target_date = null
    await load()
  } catch (e) { ElMessage.error(e.message || '目标创建失败') } finally { savingGoal.value = false }
}

async function delGoal(id) {
  await ElMessageBox.confirm('删除后引用该目标的进行中计划会一并归档，且影响评估/预测。确定删除？', '删除比赛目标', { type: 'warning' })
  await api.del(`/athlete/goals/${id}`)
  ElMessage.success('已删除')
  await load()
}
</script>

<style scoped>
.field-tip { font-size: 11px; color: var(--text-3); line-height: 1.5; margin-top: 3px; }
.hdr-tip { font-size: 11px; color: var(--text-3); margin-left: 10px; font-weight: 400; }
.auto-row { display: flex; align-items: center; gap: 7px; margin-top: 5px; }
.auto-label { font-size: 12px; color: var(--text-2); }
.auto-note { font-size: 11px; color: var(--text-3); }
.src-badge { display: inline-block; font-size: 10px; font-weight: 700; padding: 1px 6px;
             border-radius: 7px; margin-right: 5px; white-space: nowrap; }
.src-measured { color: #4ade80; background: rgba(74,222,128,.14); }
.src-derived { color: var(--blue); background: rgba(96,165,250,.14); }
.src-estimated { color: #ffa24d; background: rgba(255,162,77,.14); }
.src-user { color: var(--text-2); background: rgba(148,163,184,.16); }
.src-unknown { color: #ff6b6b; background: rgba(255,107,107,.14); }
.legend { font-size: 12px; color: var(--text-2); line-height: 1.9; }
.lg-row { display: flex; align-items: baseline; gap: 2px; }
.goal-row { display: flex; align-items: center; justify-content: space-between; padding: 9px 0; border-bottom: 1px dashed rgba(148,163,184,.12); }
.goal-main { flex: 1; min-width: 0; padding-right: 10px; }
.goal-title-line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.goal-badge { font-size: 11px; font-weight: 700; color: var(--lime); padding: 1px 7px; border-radius: 8px; background: rgba(200,241,105,.12); }
.goal-badge.done { color: #facc15; background: rgba(250,204,21,.16); }
.goal-badge.paused { color: var(--text-3); background: rgba(148,163,184,.14); }
.goal-meta { font-size: 12px; color: var(--text-3); margin-top: 3px; }
.goal-right { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.goal-ring { width: 34px; height: 34px; transform: rotate(-90deg); }
.ring-track { stroke: rgba(148,163,184,.2); stroke-width: 3.2; }
.ring-fill { stroke: var(--blue); stroke-width: 3.2; stroke-linecap: round; transition: stroke-dasharray .6s ease; }
.goal-ring.done .ring-fill { stroke: #facc15; }
.next-steps { font-size: 13px; color: var(--text-2); line-height: 2; padding-left: 18px; margin: 10px 0 0; }
</style>
