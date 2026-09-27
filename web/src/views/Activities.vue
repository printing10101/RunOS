<template>
  <div>
    <el-card shadow="never">
      <div style="display:flex; gap:10px; margin-bottom:12px">
        <el-radio-group v-model="sport" size="small" @change="onFilterChange">
          <el-radio-button value="">全部</el-radio-button>
          <el-radio-button value="run">跑步</el-radio-button>
          <el-radio-button value="ride">骑行</el-radio-button>
          <el-radio-button value="strength">力量</el-radio-button>
          <el-radio-button value="swim">游泳</el-radio-button>
        </el-radio-group>
        <el-date-picker v-model="dateRange" type="daterange" size="small" range-separator="至"
                        start-placeholder="开始日期" end-placeholder="结束日期"
                        value-format="YYYY-MM-DD" clearable style="width:240px"
                        @change="onFilterChange" />
        <el-button size="small" round type="primary" plain style="margin-left:8px" @click="openEntry">＋ 手动录入</el-button>
        <el-pagination layout="prev, pager, next" :total="total" :page-size="50" v-model:current-page="page"
                       style="margin-left:auto" @current-change="load" />
      </div>
      <el-table :data="items" size="middle" @row-click="openDetail" class="click-table" v-loading="loading"
                 empty-text="暂无训练记录，点击「＋ 手动录入」或到「平台连接」同步设备数据">
        <el-table-column label="日期" width="100">
          <template #default="{ row }">{{ row.start_time?.slice(5, 10).replace('-', '/') }}</template>
        </el-table-column>
        <el-table-column label="类型" width="70">
          <template #default="{ row }">
            <el-tag size="small" :type="{ run: 'success', ride: 'warning', strength: '', swim: 'info' }[row.sport] || 'info'">
              {{ { run: '跑步', ride: '骑行', strength: '力量', swim: '游泳', walk: '步行', other: '其他' }[row.sport] }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="title" label="标题" min-width="170" show-overflow-tooltip />
        <el-table-column label="距离" width="85" align="right">
          <template #default="{ row }">{{ row.distance_m ? (row.distance_m / 1000).toFixed(2) : '-' }}</template>
        </el-table-column>
        <el-table-column label="配速" width="80" align="right">
          <template #default="{ row }">{{ row.pace_sec_per_km ? fmtPace(row.pace_sec_per_km) : '-' }}</template>
        </el-table-column>
        <el-table-column label="时长" width="70" align="right">
          <template #default="{ row }">{{ fmtDuration(row.duration_sec) }}</template>
        </el-table-column>
        <el-table-column label="心率" width="80" align="right">
          <template #default="{ row }">
            <span v-if="row.avg_hr">{{ row.avg_hr }}<span class="dim">/{{ row.max_hr ?? '-' }}</span></span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="步频" width="65" align="right">
          <template #default="{ row }">{{ row.avg_cadence ? Math.round(row.avg_cadence) : '-' }}</template>
        </el-table-column>
        <el-table-column label="爬升" width="70" align="right">
          <template #default="{ row }">{{ row.elevation_m ? Math.round(row.elevation_m) + 'm' : '-' }}</template>
        </el-table-column>
        <el-table-column label="卡路里" width="75" align="right">
          <template #default="{ row }">{{ row.calories ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="负荷" width="65" align="right">
          <template #default="{ row }">{{ row.training_load ? Math.round(row.training_load) : '-' }}</template>
        </el-table-column>
        <el-table-column label="TE" width="70" align="right">
          <template #default="{ row }">
            <span v-if="row.te_aerobic != null" class="dim">A{{ row.te_aerobic.toFixed(1) }}
              <template v-if="row.te_anaerobic != null"> / 无{{ row.te_anaerobic.toFixed(1) }}</template></span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="跑鞋" width="90">
          <template #default="{ row }">
            <span v-if="row.gear_name" class="dim">👟 {{ row.gear_name }}</span>
            <span v-else class="dim">-</span>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="70">
          <template #default="{ row }">
            <el-tag size="small" type="info" effect="plain">{{ { coros: '高驰', garmin: '佳明', manual: '手动' }[row.platform] }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" align="center">
          <template #default="{ row }">
            <el-button size="small" round plain type="primary" @click.stop="showRowCard(row)">成绩卡</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 手动录入弹窗 -->
    <el-dialog v-model="entryVisible" title="手动录入训练" width="480px">
      <el-form :model="form" label-width="96px" size="default">
        <div class="form-grid">
          <el-form-item label="运动类型">
            <el-select v-model="form.sport" style="width:100%">
              <el-option v-for="s in ['run', 'ride', 'swim', 'strength', 'walk', 'other']" :key="s"
                         :value="s" :label="{ run: '跑步', ride: '骑行', swim: '游泳', strength: '力量', walk: '步行', other: '其他' }[s]" />
            </el-select>
          </el-form-item>
          <el-form-item label="标题"><el-input v-model="form.title" placeholder="如：晨跑" /></el-form-item>
          <el-form-item label="日期时间">
            <el-date-picker v-model="form.start_time" type="datetime" format="YYYY-MM-DD HH:mm"
                            date-format="YYYY-MM-DD" time-format="HH:mm" style="width:100%" />
          </el-form-item>
          <el-form-item label="时长(分钟)"><el-input-number v-model="form.duration_min" :min="1" :max="600" controls-position="right" style="width:100%" /></el-form-item>
          <el-form-item label="距离(km)"><el-input-number v-model="form.distance_km" :min="0" :max="500" :precision="2" controls-position="right" style="width:100%" /></el-form-item>
          <el-form-item label="平均心率"><el-input-number v-model="form.avg_hr" :min="60" :max="220" controls-position="right" style="width:100%" /></el-form-item>
          <el-form-item label="最大心率"><el-input-number v-model="form.max_hr" :min="80" :max="230" controls-position="right" style="width:100%" /></el-form-item>
          <el-form-item label="步频(spm)"><el-input-number v-model="form.avg_cadence" :min="0" :max="240" controls-position="right" style="width:100%" /></el-form-item>
          <el-form-item label="爬升(m)"><el-input-number v-model="form.elevation_m" :min="0" :max="9999" controls-position="right" style="width:100%" /></el-form-item>
          <el-form-item label="卡路里"><el-input-number v-model="form.calories" :min="0" :max="9999" controls-position="right" style="width:100%" /></el-form-item>
          <el-form-item label="跑鞋">
            <el-select v-model="form.gear_id" clearable placeholder="选择跑鞋（累计里程）" style="width:100%">
              <el-option v-for="g in shoes" :key="g.id" :value="g.id" :label="`${g.name}（${g.total_km}km）`" />
            </el-select>
          </el-form-item>
        </div>
      </el-form>
      <template #footer>
        <el-button round @click="entryVisible = false">取消</el-button>
        <el-button type="primary" round @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!-- 即时成绩卡：保存成功后立刻给出的完成反馈（借鉴 NRC 完成即激励） -->
    <el-dialog v-model="showCard" width="400px" :show-close="false"
               :close-on-click-modal="false" class="result-card-dialog">
      <div class="rc-inner" :class="{ pb: recordCard?.new_pb }">
        <div class="rc-emojis" v-if="recordCard">
          <span class="rc-mega">{{ recordCard.new_pb ? '🏆' : recordCard.emoji }}</span>
          <span class="rc-confetti">{{ recordCard.new_pb ? '🎉' : '✨' }}</span>
        </div>
        <div class="rc-type">{{ recordCard?.type_label }}成绩</div>
        <div class="rc-headline">{{ recordCard?.headline }}</div>

        <div v-if="recordCard?.highlight" class="rc-highlight">
          <span class="rc-hl-label">{{ recordCard.highlight.label }}</span>
          <span class="rc-hl-value">{{ recordCard.highlight.value }}</span>
        </div>

        <div class="rc-metrics">
          <div v-for="m in recordCard?.metrics || []" :key="m.label" class="rc-metric">
            <div class="rc-metric-value">{{ m.value }}</div>
            <div class="rc-metric-label">{{ m.label }}</div>
          </div>
        </div>

        <div v-if="recordCard?.note" class="rc-note">↔ {{ recordCard.note }}</div>
      </div>
      <template #footer>
        <el-button type="primary" round style="width:100%" @click="showCard = false">收到，继续加油</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, fmtPace } from '../api'

const router = useRouter()
const items = ref([])
const total = ref(0)
const sport = ref('')
const dateRange = ref(null)
const page = ref(1)
const entryVisible = ref(false)
const loading = ref(false)
const form = ref({})
const shoes = ref([])
const showCard = ref(false)
const recordCard = ref(null)

function fmtDuration(sec) {
  const m = Math.round((sec || 0) / 60)
  return m >= 60 ? `${Math.floor(m / 60)}h${m % 60}` : `${m}'`
}

function loadShoes() {
  api.get('/gear').then(d => {
    shoes.value = d.items.filter(g => g.kind === 'shoe' && g.status === 'active')
  }).catch(() => {})
}

function onFilterChange() {
  page.value = 1
  load()
}

function load() {
  loading.value = true
  const params = { limit: 50, offset: (page.value - 1) * 50, sport: sport.value }
  if (dateRange.value?.length === 2) {
    params.date_from = dateRange.value[0]
    params.date_to = dateRange.value[1]
  }
  api.get('/activities', params)
    .then(d => { items.value = d.items; total.value = d.total })
    .catch(() => { ElMessage.error('加载训练记录失败') })
    .finally(() => { loading.value = false })
}

function openDetail(row) {
  router.push(`/activities/${row.id}`)
}

// 对任意历史活动（含同步的）取即时成绩卡
async function showRowCard(row) {
  try {
    recordCard.value = await api.get(`/activities/${row.id}/card`)
    showCard.value = true
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '拉取成绩卡失败')
  }
}

function openEntry() {
  form.value = { sport: 'run', title: '', start_time: new Date(), duration_min: 30, distance_km: 5, gear_id: null }
  loadShoes()
  entryVisible.value = true
}

async function save() {
  const f = form.value
  if (!f.start_time || !f.duration_min) { ElMessage.warning('请填写日期与时长'); return }
  try {
    const saved = await api.post('/activities', {
      sport: f.sport, title: f.title || '手动录入', start_time: f.start_time,
      duration_sec: Math.round(f.duration_min * 60), distance_m: Math.round((f.distance_km || 0) * 1000),
      avg_hr: f.avg_hr || null, max_hr: f.max_hr || null, avg_cadence: f.avg_cadence || null,
      elevation_m: f.elevation_m || 0, calories: f.calories || null,
      gear_id: f.gear_id || null,
    })
    entryVisible.value = false
    load()
    if (saved?.record_card) {
      recordCard.value = saved.record_card
      showCard.value = true
    } else {
      ElMessage.success('已录入')
    }
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '录入失败')
  }
}

onMounted(load)
</script>

<style scoped>
.click-table :deep(.el-table__row) { cursor: pointer; }
.dim { color: var(--text-3); font-size: 12px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; }

.result-card-dialog :deep(.el-dialog__body) { padding-bottom: 6px; }
.rc-inner {
  text-align: center; padding: 8px 4px 2px;
  border: 1px solid var(--border); border-radius: 16px;
  background: linear-gradient(180deg, rgba(63, 208, 164, 0.08), transparent);
}
.rc-inner.pb { border-color: rgba(250, 204, 21, 0.4); background: linear-gradient(180deg, rgba(250, 204, 21, 0.12), transparent); }
.rc-emojis { position: relative; font-size: 34px; line-height: 1.1; }
.rc-mega { font-size: 46px; }
.rc-confetti { position: absolute; right: 12%; top: -2px; font-size: 20px; }
.rc-type { color: var(--text-3); font-size: 12px; letter-spacing: .12em; margin-top: 6px; }
.rc-headline { font-size: 17px; font-weight: 800; margin: 6px 0 12px; color: var(--text-1); }
.rc-highlight { margin-bottom: 12px; }
.rc-hl-label { display: block; color: var(--text-3); font-size: 11px; margin-bottom: 2px; }
.rc-hl-value { font-family: var(--font-display); font-size: 30px; font-weight: 800; color: var(--jade); }
.pb .rc-hl-value { color: #d4b06a; }
.rc-metrics { display: flex; gap: 8px; }
.rc-metric { flex: 1; border-radius: 10px; background: var(--bg-inset); padding: 8px 4px; border: 1px solid var(--border); }
.rc-metric-value { font-family: var(--font-display); font-size: 14px; font-weight: 700; }
.rc-metric-label { color: var(--text-3); font-size: 11px; margin-top: 2px; }
.rc-note { color: var(--text-2); font-size: 12.5px; margin-top: 12px; }
</style>
