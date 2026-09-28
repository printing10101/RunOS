<template>
  <TabPage title="训练记录" sub="活动、比赛成绩与装备里程 —— 比赛成绩是预测校准的真值锚点">
    <el-tabs v-model="tab">
      <el-tab-pane label="活动" name="activities" lazy>
        <Activities />
      </el-tab-pane>

      <el-tab-pane label="比赛成绩" name="races" lazy>
        <div style="display:flex; justify-content:flex-end; margin-bottom:10px">
          <el-button type="primary" round @click="openRace()">＋ 录入比赛成绩</el-button>
        </div>
        <el-table :data="races" size="default">
          <el-table-column prop="date" label="日期" width="110" />
          <el-table-column prop="race_name" label="赛事" min-width="140" />
          <el-table-column label="项目" width="90">
            <template #default="{ row }">{{ raceTypeLabel(row.race_type) }}</template>
          </el-table-column>
          <el-table-column prop="time_str" label="成绩" width="100" />
          <el-table-column prop="pace_str" label="配速" width="100" />
          <el-table-column label="赛前预测 / 偏差" width="150">
            <template #default="{ row }">
              <template v-if="row.prediction">
                <div>{{ row.prediction.predicted_str }}</div>
                <div style="font-size:12px" :style="{ color: deltaColor(row.prediction.delta_pct) }">
                  {{ deltaLabel(row.prediction.delta_pct) }}
                </div>
              </template>
              <span v-else style="color:var(--text-3)">-</span>
            </template>
          </el-table-column>
          <el-table-column label="VDOT" width="80">
            <template #default="{ row }">{{ row.vdot ?? '-' }}</template>
          </el-table-column>
          <el-table-column label="类型" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="row.is_official ? 'success' : 'info'" effect="plain">
                {{ row.is_official ? '正式' : '自测' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="" width="70" align="right">
            <template #default="{ row }">
              <el-button link type="danger" @click="removeRace(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>

        <el-dialog v-model="raceVisible" title="录入比赛成绩" width="480px">
          <el-form :model="raceForm" label-width="90px">
            <el-form-item label="日期">
              <el-date-picker v-model="raceForm.date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
            </el-form-item>
            <el-form-item label="赛事名称"><el-input v-model="raceForm.race_name" placeholder="可空" /></el-form-item>
            <el-form-item label="项目">
              <el-select v-model="raceForm.race_type" style="width:100%">
                <el-option v-for="rt in ['800m', '1k', '1500m', '3k', '5k', '10k', 'hm', 'marathon', 'other']" :key="rt"
                           :label="raceTypeLabel(rt)" :value="rt" />
              </el-select>
            </el-form-item>
            <el-form-item label="距离 (米)">
              <el-input-number v-model="raceForm.distance_m" :min="100" :step="100" controls-position="right" style="width:100%" />
            </el-form-item>
            <el-form-item label="成绩 (秒)">
              <el-input-number v-model="raceForm.time_sec" :min="1" controls-position="right" style="width:100%" />
            </el-form-item>
            <el-form-item label="均心率"><el-input-number v-model="raceForm.avg_hr" :min="80" :max="220" controls-position="right" /></el-form-item>
            <el-form-item label="备注"><el-input v-model="raceForm.notes" /></el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="raceVisible = false">取消</el-button>
            <el-button type="primary" :loading="raceSaving" @click="saveRace">保存</el-button>
          </template>
        </el-dialog>
      </el-tab-pane>

      <el-tab-pane label="装备" name="gear" lazy>
        <div style="display:flex; justify-content:flex-end; margin-bottom:10px">
          <el-button type="primary" round @click="openGear()">＋ 添加装备</el-button>
        </div>
        <el-row :gutter="12">
          <el-col :span="8" v-for="g in gears" :key="g.id" style="margin-bottom:12px">
            <el-card shadow="never">
              <div style="display:flex; justify-content:space-between; align-items:center">
                <b>{{ g.name }}</b>
                <el-tag size="small" :type="g.status === 'active' ? 'success' : 'info'" effect="plain">
                  {{ g.status === 'active' ? '使用中' : '已退役' }}
                </el-tag>
              </div>
              <div style="color:var(--text-3); font-size:12px; margin:4px 0 8px">
                {{ g.brand || '—' }} · 累计 {{ g.total_km }}km / 退役 {{ g.retire_km }}km
              </div>
              <el-progress :percentage="g.wear_pct" :stroke-width="8"
                           :color="g.flag === 'overdue' ? '#e05f5f' : g.flag === 'warning' ? '#d9a24e' : '#5fc987'" />
              <div style="font-size:12px; margin-top:6px; color:var(--text-2)">
                {{ g.flag_note || `剩余约 ${g.remaining_km}km` }}
              </div>
              <div v-if="g.injury" class="injury-tip">⚠ {{ g.injury.msg }}</div>
              <div style="margin-top:8px; display:flex; gap:8px">
                <el-button v-if="g.status === 'active'" size="small" @click="retireGear(g)">退役</el-button>
                <el-button size="small" type="danger" plain @click="removeGear(g)">删除</el-button>
              </div>
            </el-card>
          </el-col>
        </el-row>
        <el-empty v-if="!gears.length" description="还没有装备记录：添加跑鞋并在活动里关联，即可跟踪里程与更换提醒" />

        <el-dialog v-model="gearVisible" title="添加装备" width="460px">
          <el-form :model="gearForm" label-width="110px">
            <el-form-item label="名称"><el-input v-model="gearForm.name" placeholder="如：缓震训练鞋" /></el-form-item>
            <el-form-item label="类型">
              <el-select v-model="gearForm.kind" style="width:100%">
                <el-option label="跑鞋" value="shoe" /><el-option label="手表" value="watch" /><el-option label="其他" value="other" />
              </el-select>
            </el-form-item>
            <el-form-item label="品牌"><el-input v-model="gearForm.brand" /></el-form-item>
            <el-form-item label="启用时里程 (km)">
              <el-input-number v-model="gearForm.initial_km" :min="0" controls-position="right" />
            </el-form-item>
            <el-form-item label="退役里程 (km)">
              <el-input-number v-model="gearForm.retire_km" :min="100" :step="50" controls-position="right" />
            </el-form-item>
            <el-form-item label="备注"><el-input v-model="gearForm.notes" /></el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="gearVisible = false">取消</el-button>
            <el-button type="primary" :loading="gearSaving" @click="saveGear">保存</el-button>
          </template>
        </el-dialog>
      </el-tab-pane>
    </el-tabs>
  </TabPage>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import TabPage from '../components/TabPage.vue'
import Activities from './Activities.vue'
import { api } from '../api'
import { todayStr } from '../utils/common'

const tab = ref('activities')

const RACE_TYPES = { '800m': '800米', '1k': '1公里', '1500m': '1500米', '3k': '3公里',
                    '5k': '5公里', '10k': '10公里', hm: '半马', marathon: '全马', other: '其他' }
const raceTypeLabel = (t) => RACE_TYPES[t] || t

// 偏差复盘：正值 = 比预测慢。±2% 内视为模型精准
function deltaLabel(pct) {
  if (pct == null) return ''
  const v = Math.abs(pct)
  const word = pct > 0 ? `慢 ${v}%` : pct < 0 ? `快 ${v}%` : '与预测一致'
  return v <= 2 ? `${word} · 精准` : word
}
const deltaColor = (pct) => {
  if (pct == null) return 'var(--text-2)'
  const v = Math.abs(pct)
  return v <= 2 ? 'var(--jade)' : v <= 5 ? 'var(--text-2)' : 'var(--orange)'
}

// ---- 比赛成绩 ----
const races = ref([])
const raceVisible = ref(false)
const raceSaving = ref(false)
const raceForm = ref({})

async function loadRaces() {
  races.value = (await api.get('/races')).races || []
}
function openRace() {
  raceForm.value = { date: todayStr(), race_name: '', race_type: 'other',
                     distance_m: 5000, time_sec: 1500, avg_hr: null, notes: '' }
  raceVisible.value = true
}
async function saveRace() {
  raceSaving.value = true
  try {
    await api.post('/races', raceForm.value)
    ElMessage.success('已录入，成绩预测将自动校准')
    raceVisible.value = false
    await loadRaces()
  } catch (e) { ElMessage.error(e.message || '保存失败') } finally { raceSaving.value = false }
}
async function removeRace(row) {
  await api.del(`/races/${row.id}`)
  await loadRaces()
}

// ---- 装备 ----
const gears = ref([])
const gearVisible = ref(false)
const gearSaving = ref(false)
const gearForm = ref({})

async function loadGear() {
  gears.value = (await api.get('/gear')).gears || []
}
function openGear() {
  gearForm.value = { name: '', kind: 'shoe', brand: '', initial_km: 0, retire_km: 600, notes: '' }
  gearVisible.value = true
}
async function saveGear() {
  gearSaving.value = true
  try {
    await api.post('/gear', gearForm.value)
    ElMessage.success('已添加')
    gearVisible.value = false
    await loadGear()
  } catch (e) { ElMessage.error(e.message || '保存失败') } finally { gearSaving.value = false }
}
async function retireGear(g) {
  await api.post(`/gear/${g.id}/retire`, {})
  await loadGear()
}
async function removeGear(g) {
  await api.del(`/gear/${g.id}`)
  await loadGear()
}

onMounted(() => { loadRaces(); loadGear() })
</script>

<style scoped>
.injury-tip {
  margin-top: 8px; padding: 8px 10px; border-radius: 8px; font-size: 12px; line-height: 1.6;
  background: rgba(224, 95, 95, 0.1); border: 1px solid rgba(224, 95, 95, 0.35);
  color: #ea8a8a;
}
</style>
