<template>
  <div>
    <el-row :gutter="14">
      <el-col :span="12" v-for="p in platformCards" :key="p.key">
        <el-card shadow="never" class="plat-card">
          <div style="display:flex; align-items:center; gap:12px">
            <div class="plat-icon">{{ p.icon }}</div>
            <div style="flex:1">
              <div style="font-weight:700; font-size:15px">{{ p.name }}
                <el-tag v-if="connOf(p.key)" type="success" size="small" style="margin-left:6px">已连接</el-tag>
                <el-tag v-else-if="pendingOf(p.key)" type="warning" size="small" style="margin-left:6px">待授权</el-tag>
              </div>
              <div style="font-size:12px; color:var(--text-3); margin-top:2px">{{ p.desc }}</div>
            </div>
          </div>

          <div v-if="connOf(p.key)" style="margin-top:12px; display:flex; align-items:center; gap:8px; flex-wrap:wrap">
            <span style="font-size:12px; color:var(--text-3)">
              最近同步：{{ connOf(p.key).last_sync_at?.slice(0, 16).replace('T', ' ') || '从未' }}
            </span>
            <el-select v-model="syncRange" size="small" style="width:104px">
              <el-option :value="90" label="近 90 天" />
              <el-option :value="180" label="近半年" />
              <el-option :value="365" label="近一年" />
            </el-select>
            <el-button size="small" type="primary" :loading="syncing === p.key" @click="sync(p.key)">同步数据</el-button>
            <el-button v-if="p.key === 'coros'" size="small" plain
                       :loading="syncingSchedule" @click="syncSchedule">仅同步课表</el-button>
            <el-button v-if="p.key === 'coros'" size="small" @click="showTools">可用数据接口</el-button>
            <el-button v-if="p.key === 'coros' && connOf(p.key)" size="small" type="success" plain
                       :loading="loadingFitness" @click="showFitness">官方体能评估</el-button>
            <el-button size="small" @click="disconnect(p.key)">断开</el-button>
          </div>
          <div v-else style="margin-top:12px; display:flex; gap:8px; flex-wrap:wrap">
            <el-button size="small" type="primary" :loading="authing === p.key" @click="connectOfficial(p.key)">
              {{ p.action }}
            </el-button>
            <el-button v-if="p.key === 'coros' && pendingOf(p.key)" size="small" @click="refresh">我已授权，刷新状态</el-button>
          </div>

          <el-alert v-if="p.needConfig && !conn.config_ready?.[p.key]" type="warning"
                    :title="p.needConfig" :closable="false" show-icon style="margin-top:10px" font-size="12" />
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <div style="display:flex; align-items:center; justify-content:space-between">
              <span>自动更新</span>
              <el-switch v-model="auto.enabled" @change="saveAuto" :disabled="savingAuto" />
            </div>
          </template>
          <div style="display:flex; align-items:center; gap:10px; margin-bottom:10px">
            <span style="font-size:13px; color:var(--text-2)">每</span>
            <el-input-number v-model="auto.minutes" :min="4" :max="1440" :step="1" size="small"
                             controls-position="right" style="width:110px" @change="saveAuto" :disabled="savingAuto" />
            <span style="font-size:13px; color:var(--text-2)">分钟自动同步一次新数据</span>
          </div>
          <div style="font-size:12px; color:var(--text-3); line-height:1.8">
            平台运行期间，后台自动为已连接平台增量拉取新活动与身体数据（评估/负荷/计划随数据自动刷新）。
            高驰官方不支持推送，定时拉取即「实时更新」的实现方式；设置跨重启生效。
            最快 4 分钟一档，新数据最坏 5 分钟内入库；身体数据（睡眠/心率）自动按至少 30 分钟一次降频拉取。
          </div>
          <div v-for="(r, platform) in auto.last_run" :key="platform" style="margin-top:8px; font-size:12px">
            <el-tag size="small" :type="r.ok ? 'success' : 'danger'" effect="plain">
              {{ platformName(platform) }} {{ r.at?.slice(11, 16) }}
            </el-tag>
            <span style="color:var(--text-3); margin-left:6px">
              {{ r.ok ? `自动新增 ${r.added ?? 0} 条活动${r.body_days ? `、身体数据 ${r.body_days} 天` : ''}` : `失败：${r.error}` }}
            </span>
          </div>
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card shadow="never">
          <template #header>同步说明</template>
          <ol class="sync-guide">
            <li>高驰走<b>官方 MCP 通道</b>：点「连接高驰」→ 浏览器登录高驰账号并授权 → 回到本页点「同步数据」，<b>一次完成</b>官方课表镜像 + 训练记录/身体数据拉取（「仅同步课表」只刷新课表、不拉记录）；</li>
            <li>可读取运动记录、分圈分段、心率/配速/海拔、睡眠与 HRV、静息心率、训练负荷、体能评估（VO2max / 阈值配速 / 赛事预测）；</li>
            <li>还可按需拉取 <b>FIT 原始文件</b>（含 GPS 轨迹与逐秒数据，官方限每账号每日 50 条）；</li>
            <li>无法走 API 时，可在「训练计划」页下载 <b>FIT 课表文件</b>，在高驰 App 内导入。</li>
          </ol>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="14" style="margin-top:14px">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>高驰官方 MCP 通道说明</template>
          <ol class="sync-guide">
            <li>高驰官方开放了 <b>Build on COROS MCP</b>：<b>无需申请、无需审批、无需企业资质</b>；</li>
            <li>平台通过 OAuth 2.0（授权码 + PKCE + 动态客户端注册）接入，你只需在高驰登录页授权一次；</li>
            <li>数据范围仅限<b>你自己的账号</b>，随时可在高驰 App 内撤销授权；</li>
            <li>官方限制：不支持 webhook 推送，可用左侧<b>自动更新</b>定时拉取（默认每 30 分钟），或随时手动同步；</li>
            <li>训练计划<b>下发</b>到高驰手表暂未开放（官方标注 coming soon），需先在「训练计划」页导出 FIT 手动导入。</li>
          </ol>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never" class="danger-card">
          <template #header>数据重置</template>
          <div class="reset-tip">
            清空<b>全部业务数据</b>：训练活动、装备、比赛成绩、每日打卡、身体数据、力量测试、饮食、计划与评估快照。
            适合从零重新开始。<b>平台连接授权会保留</b>，跑者档案、目标与日程也会保留。
          </div>
          <el-popconfirm title="确定清空全部训练/身体/评估数据？此操作不可恢复" width="280"
                         confirm-button-text="确认清空" cancel-button-text="取消"
                         @confirm="resetData">
            <template #reference>
              <el-button size="small" type="danger" plain :loading="resetting">清除演示 / 历史数据</el-button>
            </template>
          </el-popconfirm>
        </el-card>
      </el-col>
    </el-row>

    <el-dialog v-model="toolsVisible" title="高驰 MCP 可用数据接口" width="640">
      <div v-if="loadingTools" style="text-align:center; padding:20px">
        <el-icon class="is-loading"><Loading /></el-icon> 正在读取…
      </div>
      <el-table v-else :data="tools" size="small" max-height="420">
        <el-table-column prop="name" label="接口" width="240" />
        <el-table-column prop="description" label="说明" show-overflow-tooltip />
      </el-table>
    </el-dialog>

    <el-dialog v-model="fitnessVisible" title="高驰官方体能评估（实时）" width="640">
      <div v-if="loadingFitness" style="text-align:center; padding:20px">
        <el-icon class="is-loading"><Loading /></el-icon> 正在从高驰读取…
      </div>
      <template v-else-if="fitness">
        <div class="fit-grid" v-if="fitnessKv.length">
          <div v-for="row in fitnessKv" :key="row.k" class="fit-row">
            <span class="fit-k">{{ row.k }}</span>
            <b class="fit-v">{{ row.v }}</b>
          </div>
        </div>
        <template v-if="loadKv">
          <div class="fit-sub">最新训练负荷评估（{{ loadKv.date }}）· 高驰官方口径</div>
          <div class="fit-grid">
            <div class="fit-row"><span class="fit-k">状态评价</span><b class="fit-v">{{ loadKv.comment }}</b></div>
            <div class="fit-row"><span class="fit-k">短期负荷</span><b class="fit-v">{{ loadKv.short }}</b></div>
            <div class="fit-row"><span class="fit-k">长期负荷</span><b class="fit-v">{{ loadKv.long }}</b></div>
            <div class="fit-row"><span class="fit-k">负荷比</span><b class="fit-v">{{ loadKv.ratio }}</b></div>
          </div>
        </template>
        <el-alert v-if="!fitnessKv.length && !loadKv" type="warning" title="高驰未返回评估数据（可能训练数据不足）" :closable="false" />
        <div class="fit-note">口径说明：官方 VO2max 由手表按「配速 + 心率」估算生理潜能；平台 VDOT 由实际成绩反推（你日常多为轻松跑，未全力测过，故偏低）。两者一个看潜能、一个看已验证成绩，不矛盾。</div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'
import { platName } from '../utils/common'

const conn = ref({})
const syncing = ref('')
const authing = ref('')
const resetting = ref(false)
const toolsVisible = ref(false)
const loadingTools = ref(false)
const tools = ref([])
const syncRange = ref(365)

// 自动同步设置与最近一次结果（每 30s 轮询状态）
const auto = ref({ enabled: false, minutes: 4, last_run: {} })
const savingAuto = ref(false)
let autoTimer = null

async function loadAuto() {
  try { auto.value = { ...auto.value, ...(await api.get('/connections/auto')) } } catch { /* ignore */ }
}

async function saveAuto() {
  savingAuto.value = true
  try {
    auto.value = { ...auto.value, ...(await api.post('/connections/auto', {
      enabled: auto.value.enabled, minutes: auto.value.minutes,
    })) }
    ElMessage.success(auto.value.enabled
      ? `自动同步已开启：每 ${auto.value.minutes} 分钟`
      : '自动同步已关闭')
  } catch (e) {
    ElMessage.error(e.message || '设置失败')
    loadAuto()
  } finally { savingAuto.value = false }
}

function platformName(key) { return { coros: '高驰', garmin: '佳明', strava: 'Strava' }[key] || key }

const platformCards = [
  { key: 'coros', name: '高驰 COROS（官方 MCP）', icon: '⌚', action: '连接高驰',
    desc: '官方 MCP 通道：读取活动 / 分圈 / 睡眠 / HRV / 训练负荷 / 体能评估，支持 FIT 原始文件',
    needConfig: '' },
  { key: 'garmin', name: '佳明 Garmin', icon: '⌚', action: '官方接口接入',
    desc: 'Garmin Connect：拉取活动详情 + 每日 HRV/睡眠/身体电量',
    needConfig: '需在 backend/.env 配置 GARMIN_EMAIL / GARMIN_PASSWORD，并 pip install garminconnect' },
  { key: 'strava', name: 'Strava（备选通道）', icon: '🔗', action: '授权接入',
    desc: '官方 API：拉取真实分段 / 逐点曲线 / GPS 轨迹',
    needConfig: '需在 strava.com/settings/api 免费创建应用，backend/.env 配置 STRAVA_CLIENT_ID/SECRET' },
]

// 只有 status=connected 才算已连接（授权中途会写入 status=pending 的占位记录）
function connOf(key) {
  return (conn.value.connections || []).find(c => c.platform === key && c.status === 'connected')
}
function pendingOf(key) {
  return (conn.value.connections || []).find(c => c.platform === key && c.status === 'pending')
}

async function load() { conn.value = await api.get('/connections') }
function refresh() { load() }

// 授权在浏览器新窗口完成，平台页轮询等待授权结果
let pollTimer = null
function startPoll() {
  stopPoll()
  let rounds = 0
  pollTimer = setInterval(async () => {
    rounds += 1
    try {
      await load()
    } catch {
      return   // 后端抖动不中断轮询；错误已由全局兜底提示，避免 3s 一条 unhandled rejection
    }
    if (connOf('coros')) {
      stopPoll()
      ElMessage.success('高驰账号已连接，可以点击「同步数据」')
    } else if (rounds > 100) {
      stopPoll()
      ElMessage.warning('等待授权超时（5 分钟）：若已在高驰完成授权，请手动刷新本页')
    }
  }, 3000)
}
function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null } }
onUnmounted(stopPoll)

async function connectOfficial(key) {
  authing.value = key
  try {
    if (key === 'coros' || key === 'strava') {
      const { url } = await api.post(`/connections/${key}/authorize-url`)
      window.open(url, '_blank')
      if (key === 'coros') {
        ElMessage.info('已打开高驰授权页，请登录并点击授权；完成后本页会自动刷新')
        startPoll()
      }
    } else {
      await api.post('/connections/connect', { platform: key, mode: 'official', credentials: {} })
      ElMessage.info('已在后端校验凭据；请重启后端使 .env 生效后再同步')
    }
    await load()
  } catch (e) {
    ElMessage.error(e.message || '连接失败')
  } finally { authing.value = '' }
}

async function sync(key) {
  syncing.value = key
  let scheduleNote = ''
  try {
    // 高驰：一次点按完成两件事——先镜像官方课表（秒级），再拉训练记录（分钟级）。
    // 课表镜像失败只提示不阻断，训练记录才是这个按钮的主事。
    if (key === 'coros') scheduleNote = await syncScheduleQuiet()
    // 近一年全量同步要分 6-7 段拉取 + 补详情，耗时可达数分钟，覆盖全局 30s 超时
    const r = await api.post(`/connections/${key}/sync`, { since_days: syncRange.value, detail_limit: 30 },
                             { timeout: 600000 })
    ElMessage.success(`同步完成：拉取 ${r.fetched} 条，新增 ${r.added} 条` +
                      (r.body_days ? `，身体数据 ${r.body_days} 天` : '') +
                      (scheduleNote ? `；${scheduleNote}` : ''))
    await load()
  } catch (e) {
    ElMessage.error(e.message || '同步失败')
  } finally { syncing.value = '' }
}

// 供「同步数据」捎带的课表镜像：有变动时返回一句摘要拼进完成提示，失败只警告
async function syncScheduleQuiet() {
  try {
    const r = await api.post('/connections/coros/sync-schedule', {}, { timeout: 120000 })
    if (!r.count || (!r.added && !r.updated && !r.removed && !r.archived_local)) return ''
    return `课表新增 ${r.added}、更新 ${r.updated}` +
           (r.removed ? `、移除 ${r.removed}` : '') +
           (r.archived_local ? '；本地原课表已归档' : '')
  } catch (e) {
    ElMessage.warning(`高驰课表镜像失败（不影响训练记录）：${e.message || '未知错误'}`)
    return ''
  }
}

// 单独刷新课表用（高驰改了手表上的课表、又不想等一次全量数据同步时）
const syncingSchedule = ref(false)

async function syncSchedule() {
  syncingSchedule.value = true
  try {
    const r = await api.post('/connections/coros/sync-schedule', {}, { timeout: 120000 })
    if (!r.count) { ElMessage.info('高驰暂无可同步的课表'); return }
    ElMessage.success(`课表已同步：共 ${r.count} 节，新增 ${r.added}、更新 ${r.updated}` +
                      (r.removed ? `、移除 ${r.removed}` : '') +
                      (r.archived_local ? '；本平台原课表已归档' : ''))
  } catch (e) {
    ElMessage.error(e.message || '课表同步失败')
  } finally { syncingSchedule.value = false }
}

async function showTools() {
  toolsVisible.value = true
  loadingTools.value = true
  try {
    const r = await api.get('/connections/coros/tools')
    tools.value = r.tools
  } catch (e) {
    ElMessage.error(e.message || '读取失败')
    toolsVisible.value = false
  } finally { loadingTools.value = false }
}

// 高驰官方体能评估：fitness/load 为接口返回的文本原文，解析成键值对展示
const fitnessVisible = ref(false)
const loadingFitness = ref(false)
const fitness = ref(null)
const fitnessKv = ref([])
const loadKv = ref(null)

function parseKv(text) {
  const out = []
  for (const line of String(text || '').split('\n')) {
    const m = line.match(/^\s*([A-Za-z0-9 /-]+?)\s*:\s*(.+?)\s*$/)
    if (m && m[1] !== 'Comment' && m[1] !== 'Date') out.push({ k: m[1], v: m[2] })
  }
  return out
}

function parseLoad(text) {
  // 取第一个日期段（最新）：2026-09-10 / Comment: x / Short-Term Load: n / Long-Term Load: n / Load Ratio: n
  const block = String(text || '').split(/\n(?=\d{4}-\d{2}-\d{2})/)[0] || ''
  const pick = (re) => { const m = block.match(re); return m ? m[1] : '' }
  const date = pick(/^\s*(\d{4}-\d{2}-\d{2})/m)
  if (!date) return null
  return {
    date,
    comment: pick(/Comment:\s*(.+)/),
    short: pick(/Short-Term Load:\s*(\d+)/),
    long: pick(/Long-Term Load:\s*(\d+)/),
    ratio: pick(/Load Ratio:\s*([\d.]+)/),
  }
}

async function showFitness() {
  fitnessVisible.value = true
  loadingFitness.value = true
  try {
    const r = await api.get('/connections/coros/fitness')
    fitness.value = r
    fitnessKv.value = parseKv(r.fitness)
    const lk = parseLoad(r.load)
    loadKv.value = lk ? { date: lk.date, comment: lk.comment, short: lk.short, long: lk.long, ratio: lk.ratio } : null
  } catch (e) {
    ElMessage.error(e.message || '读取失败')
    fitnessVisible.value = false
  } finally { loadingFitness.value = false }
}

async function disconnect(key) {
  await ElMessageBox.confirm(`断开后将删除已保存的授权凭据，再次使用需重新授权。确定断开${platName(key)}？`, '断开平台连接', { type: 'warning' })
  try {
    await api.post(`/connections/${key}/disconnect`, {})
    await load()
  } catch (e) { ElMessage.error(e.message || '断开失败') }
}

async function resetData() {
  resetting.value = true
  try {
    const r = await api.post('/connections/reset-data?confirm=true', {})
    ElMessage.success(`已清除：${Object.entries(r.cleared).filter(([, n]) => n).map(([k, n]) => `${k} ${n} 条`).join('、') || '无数据'}`)
  } catch (e) {
    ElMessage.error(e.message || '重置失败')
  } finally { resetting.value = false }
}

onMounted(() => {
  load()
  loadAuto()
  autoTimer = setInterval(loadAuto, 30000)
})
onUnmounted(() => { if (autoTimer) { clearInterval(autoTimer); autoTimer = null } })
</script>

<style scoped>
.plat-card :deep(.el-card__body) { padding: 16px 18px; }
.plat-icon { font-size: 30px; width: 48px; height: 48px; border-radius: 12px; background: rgba(63,208,164,0.1); display: flex; align-items: center; justify-content: center; }
.sync-guide { font-size: 13px; color: var(--text-2); line-height: 2; padding-left: 18px; margin: 0; }
.sync-guide code { background: rgba(157,184,173,.12); border-radius: 4px; padding: 1px 6px; font-size: 12px; }
.reset-tip { font-size: 12.5px; color: var(--text-2); line-height: 1.8; margin-bottom: 12px; }
.fit-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 18px; margin-bottom: 14px; }
.fit-row { display: flex; justify-content: space-between; align-items: baseline; padding: 7px 10px; border: 1px solid var(--border); border-radius: 8px; }
.fit-k { font-size: 12px; color: var(--text-3); }
.fit-v { font-size: 13.5px; color: var(--jade); }
.fit-sub { font-size: 12.5px; color: var(--text-2); margin: 4px 0 8px; font-weight: 700; }
.fit-note { font-size: 12px; color: var(--text-3); line-height: 1.8; border-top: 1px dashed var(--border); padding-top: 10px; margin-top: 4px; }
</style>
