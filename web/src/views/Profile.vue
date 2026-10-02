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
                {{ raceTypeName(g.race_type) }}
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
                <el-option value="800m" label="800 米" />
                <el-option value="1k" label="1 公里" />
                <el-option value="1500m" label="1500 米" />
                <el-option value="3k" label="3 公里" />
                <el-option value="5k" label="5 公里" />
                <el-option value="10k" label="10 公里" />
                <el-option value="hm" label="半程马拉松" />
                <el-option value="marathon" label="全程马拉松" />
              </el-select>
            </el-form-item>
            <el-form-item label="目标成绩">
              <el-input v-model="goalForm.time_text" placeholder="如 3:30:00 / 22:30 / 3小时30分 / 1分50秒，可不填" />
              <div v-if="timePreview" class="field-tip" style="margin-top:6px" :style="timePreviewOk ? '' : 'color:#e0805f'">
                {{ timePreview }}
              </div>
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

        <el-card shadow="never" style="margin-top:14px">
          <template #header>
            数据与备份
            <span class="hdr-tip">每次启动自动快照，滚动保留最近 7 份</span>
          </template>
          <div class="backup-actions">
            <el-button size="small" round :loading="exporting" @click="exportData">导出全部数据 (JSON)</el-button>
            <el-button size="small" round :loading="backingUp" @click="runBackup">立即备份</el-button>
          </div>
          <div v-if="backupsError" class="field-tip" style="color:#e0805f">备份列表加载失败：{{ backupsError }}</div>
          <el-empty v-else-if="!backups.length" description="还没有备份，点上面「立即备份」生成第一个还原点" :image-size="60" />
          <div v-for="b in backups" :key="b.name" class="backup-row">
            <span class="backup-time">{{ b.created_at.replace('T', ' ') }}</span>
            <span class="backup-size">{{ b.size_mb }} MB</span>
            <el-button size="small" text bg @click="downloadBackup(b.name)">下载</el-button>
          </div>
          <div class="field-tip" style="margin-top:10px">
            恢复方法：关闭 RunOS → 用备份文件覆盖 backend/sport_platform.db → 重新打开。
            备份默认与数据库同盘，建议在 backend/.env 设 BACKUP_DIR 指到另一块盘或网盘目录。
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { api, fmtTime } from '../api'
import { raceTypeName } from '../utils/common'

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
onMounted(loadBackups)
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

// 解析用户输入的目标成绩（秒）。返回整数秒；无法识别返回 NaN；空串返回 null。
// 支持格式（此前只认 冒号:数字 一种写法，"1分50秒""330""4:30 当小时"这类输入
// 会被按错误单位静默存错——现在配合下方实时预览，存什么一目了然）：
//   3:30:00 / 22:30        冒号式（时:分:秒 或 分:秒）
//   3小时30分 / 1分50秒 / 45分钟   中文单位
//   330（全马/半马下）      三位紧凑式 = 3小时30分（与 AI 对话同一口径）
//   45（纯数字）           按分钟
function parseTimeSec(text) {
  const t = (text || '').trim()
  if (!t) return null
  if (t.includes(':')) {
    const parts = t.split(':').map(x => parseInt(x, 10))
    if (parts.some(isNaN) || parts.length < 2) return NaN
    return parts.reduce((acc, v) => acc * 60 + v, 0)
  }
  const h = t.match(/(\d+)\s*小时/)
  const m = t.match(/(\d+)\s*分/)
  const s = t.match(/(\d+)\s*[秒S]/)
  if (h || m || s) {
    return (h ? +h[1] * 3600 : 0) + (m ? +m[1] * 60 : 0) + (s ? +s[1] : 0)
  }
  if (!/^\d+$/.test(t)) return NaN
  const num = parseInt(t, 10)
  // 三位数字 + 全马/半马：按 h:mm 紧凑式（330 = 3小时30分，与 AI 对话口径一致）
  if ((goalForm.race_type === 'marathon' || goalForm.race_type === 'hm') && /^\d{3}$/.test(t)) {
    return Math.floor(num / 100) * 3600 + (num % 100) * 60
  }
  return num * 60   // 其余纯数字按分钟
}

// 实时预览「这次输入会被存成什么」：解析错误时给出格式提示而不是等保存后才发现
const timePreview = computed(() => {
  const t = (goalForm.time_text || '').trim()
  if (!t) return ''
  const sec = parseTimeSec(t)
  if (sec === null) return ''
  if (Number.isNaN(sec)) return '无法识别这个写法，试试 3:30:00 / 22:30 / 3小时30分 / 1分50秒'
  return `识别为：${fmtDuration(sec)}（共 ${sec} 秒），将按此保存`
})
const timePreviewOk = computed(() => {
  const sec = parseTimeSec(goalForm.time_text)
  return sec !== null && !Number.isNaN(sec)
})

function fmtDuration(sec) {
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  const parts = []
  if (h) parts.push(`${h}小时`)
  if (m) parts.push(`${m}分`)
  if (s || !parts.length) parts.push(`${s}秒`)
  return parts.join('')
}

const savingGoal = ref(false)
async function addGoal() {
  const target_time_sec = parseTimeSec(goalForm.time_text)
  if (goalForm.time_text && (target_time_sec === null || Number.isNaN(target_time_sec))) {
    ElMessage.error('目标成绩无法识别，支持 3:30:00 / 22:30 / 3小时30分 / 1分50秒 等写法')
    return
  }
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
  const ok = await ElMessageBox.confirm('删除后引用该目标的进行中计划会一并归档，且影响评估/预测。确定删除？', '删除比赛目标', { type: 'warning' }).catch(() => false)
  if (!ok) return
  await api.del(`/athlete/goals/${id}`)
  ElMessage.success('已删除')
  await load()
}

// ---------------------------------------------------------------- 数据与备份

const backups = ref([])
const backupsError = ref('')
const backingUp = ref(false)
const exporting = ref(false)

async function loadBackups() {
  backupsError.value = ''
  try {
    backups.value = (await api.get('/system/backups')).items || []
  } catch (e) {
    backupsError.value = e.message || '未知错误'
  }
}

async function runBackup() {
  backingUp.value = true
  try {
    await api.post('/system/backups')
    ElMessage.success('已生成备份还原点')
    await loadBackups()
  } catch (e) {
    ElMessage.error(e.message || '备份失败')
  } finally { backingUp.value = false }
}

// 下载走 fetch+blob：桌面壳（WebView2）里 window.open 触发的下载体验不可控
async function _downloadFile(url, filename) {
  const resp = await fetch(url)
  if (!resp.ok) {
    let detail = ''
    try { detail = (await resp.json()).detail || '' } catch { /* 非 JSON 错误体 */ }
    throw new Error(detail || `下载失败（${resp.status}）`)
  }
  const blob = await resp.blob()
  const link = document.createElement('a')
  link.href = URL.createObjectURL(blob)
  link.download = filename
  link.click()
  URL.revokeObjectURL(link.href)
}

async function downloadBackup(name) {
  try {
    await _downloadFile(`/api/system/backups/${encodeURIComponent(name)}/download`, name)
  } catch (e) { ElMessage.error(e.message || '下载失败') }
}

async function exportData() {
  exporting.value = true
  try {
    const name = `runos_export_${new Date().toISOString().slice(0, 10)}.json`
    await _downloadFile('/api/athlete/export', name)
    ElMessage.success('已导出全部训练数据')
  } catch (e) {
    ElMessage.error(e.message || '导出失败')
  } finally { exporting.value = false }
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
.src-measured { color: #5fc987; background: rgba(95, 201, 135,.14); }
.src-derived { color: var(--blue); background: rgba(96,165,250,.14); }
.src-estimated { color: #d9a24e; background: rgba(217,162,78,.14); }
.src-user { color: var(--text-2); background: rgba(157,184,173,.16); }
.src-unknown { color: #e05f5f; background: rgba(224,95,95,.14); }
.legend { font-size: 12px; color: var(--text-2); line-height: 1.9; }
.lg-row { display: flex; align-items: baseline; gap: 2px; }
.goal-row { display: flex; align-items: center; justify-content: space-between; padding: 9px 0; border-bottom: 1px dashed rgba(157,184,173,.12); }
.goal-main { flex: 1; min-width: 0; padding-right: 10px; }
.goal-title-line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.goal-badge { font-size: 11px; font-weight: 700; color: var(--jade); padding: 1px 7px; border-radius: 8px; background: rgba(63,208,164,.12); }
.goal-badge.done { color: #d4b06a; background: rgba(250,204,21,.16); }
.goal-badge.paused { color: var(--text-3); background: rgba(157,184,173,.14); }
.goal-meta { font-size: 12px; color: var(--text-3); margin-top: 3px; }
.goal-right { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.goal-ring { width: 34px; height: 34px; transform: rotate(-90deg); }
.ring-track { stroke: rgba(157,184,173,.2); stroke-width: 3.2; }
.ring-fill { stroke: var(--blue); stroke-width: 3.2; stroke-linecap: round; transition: stroke-dasharray .6s ease; }
.goal-ring.done .ring-fill { stroke: #d4b06a; }
.next-steps { font-size: 13px; color: var(--text-2); line-height: 2; padding-left: 18px; margin: 10px 0 0; }
.backup-actions { display: flex; gap: 8px; margin-bottom: 10px; }
.backup-row { display: flex; align-items: center; gap: 10px; padding: 7px 0;
              border-bottom: 1px dashed rgba(157,184,173,.12); font-size: 12px; }
.backup-time { flex: 1; color: var(--text-2); }
.backup-size { color: var(--text-3); }
</style>
