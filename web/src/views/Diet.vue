<template>
  <TabPage title="饮食" sub="营养目标跟随当天课型：质量课多碳水、休息日控碳，能量缺口会联动训练建议">
    <el-skeleton v-if="loading" :rows="8" animated />
    <el-empty v-else-if="empty" description="请先在「设置 → 个人档案」完善身高体重，再开始记录饮食" />
    <template v-else>
      <el-row :gutter="14">
        <el-col :span="8">
          <el-card shadow="never">
            <template #header><div class="card-head">今日目标 <span class="card-sub">{{ d.today?.day_type_label }}{{ d.today?.workout_title ? ` · ${d.today.workout_title}` : '' }}</span></div></template>
            <div class="target-grid">
              <div class="tg-item"><div class="num-display tg-v">{{ d.targets?.kcal ?? '-' }}</div><div class="tg-k">千卡</div></div>
              <div class="tg-item"><div class="num-display tg-v">{{ d.targets?.protein_g ?? '-' }}</div><div class="tg-k">蛋白质 g</div></div>
              <div class="tg-item"><div class="num-display tg-v">{{ d.targets?.carb_g ?? '-' }}</div><div class="tg-k">碳水 g</div></div>
              <div class="tg-item"><div class="num-display tg-v">{{ d.targets?.fat_g ?? '-' }}</div><div class="tg-k">脂肪 g</div></div>
            </div>
            <div v-if="d.targets?.note" style="font-size:12px; color:var(--text-2); margin-top:10px; line-height:1.7">{{ d.targets.note }}</div>
            <el-alert v-if="d.fueling?.cap_level" type="warning" :closable="false" style="margin-top:10px"
                      :title="d.fueling.cap_level === 'easy' ? '持续能量缺口：今日建议只安排轻松跑' : '补给偏低：建议今日训练减量'"
                      :description="(d.fueling.reasons || []).join('；')" />
          </el-card>
        </el-col>
        <el-col :span="16">
          <el-card shadow="never">
            <template #header>
              <div class="card-head">能量平衡（近 14 天）
                <span class="card-sub">柱=摄入−目标，线=运动消耗</span>
                <el-button size="small" round style="float:right" :loading="aiEstimating" @click="askAiEstimate">AI 估算一餐</el-button>
              </div>
            </template>
            <div ref="balanceChart" style="height: 250px"></div>
          </el-card>
        </el-col>
      </el-row>

      <el-card shadow="never" style="margin-top:14px">
        <template #header>
          <div class="card-head">饮食记录 <span class="card-sub">近 14 天 · 达标率 {{ d.analysis?.kcal_rate ?? '-' }}%</span>
            <el-button size="small" type="primary" round style="float:right" @click="openLog()">＋ 记一餐</el-button>
          </div>
        </template>
        <el-table :data="d.logs || []" size="default">
          <el-table-column prop="date" label="日期" width="110" />
          <el-table-column label="餐次" width="90">
            <template #default="{ row }">{{ MEALS[row.meal] || row.meal }}</template>
          </el-table-column>
          <el-table-column prop="description" label="内容" min-width="180" />
          <el-table-column prop="kcal" label="千卡" width="90" />
          <el-table-column prop="protein_g" label="蛋白 g" width="90" />
          <el-table-column prop="carb_g" label="碳水 g" width="90" />
          <el-table-column prop="fat_g" label="脂肪 g" width="90" />
          <el-table-column label="" width="120" align="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openLog(row)">编辑</el-button>
              <el-button link type="danger" @click="removeLog(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="!(d.logs || []).length" description="还没有记录：至少记录主餐，补给状态联动才会生效" :image-size="60" />
        <div v-if="(d.analysis?.issues || []).length" style="margin-top:10px">
          <div v-for="(iss, i) in d.analysis.issues" :key="i" style="font-size:12px; color:var(--text-2); line-height:1.9">· {{ iss }}</div>
        </div>
      </el-card>

      <el-dialog v-model="logVisible" :title="logForm.id ? '修正记录' : '记一餐'" width="480px">
        <el-form :model="logForm" label-width="90px">
          <el-form-item label="日期"><el-date-picker v-model="logForm.date" type="date" value-format="YYYY-MM-DD" style="width:100%" /></el-form-item>
          <el-form-item label="餐次">
            <el-select v-model="logForm.meal" style="width:100%">
              <el-option v-for="(label, key) in MEALS" :key="key" :label="label" :value="key" />
            </el-select>
          </el-form-item>
          <el-form-item label="吃了什么"><el-input v-model="logForm.description" placeholder="如：牛肉面一碗 + 鸡蛋" /></el-form-item>
          <el-form-item label="千卡"><el-input-number v-model="logForm.kcal" :min="0" controls-position="right" /></el-form-item>
          <el-form-item label="蛋白 g"><el-input-number v-model="logForm.protein_g" :min="0" controls-position="right" /></el-form-item>
          <el-form-item label="碳水 g"><el-input-number v-model="logForm.carb_g" :min="0" controls-position="right" /></el-form-item>
          <el-form-item label="脂肪 g"><el-input-number v-model="logForm.fat_g" :min="0" controls-position="right" /></el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="logVisible = false">取消</el-button>
          <el-button type="primary" :loading="saving" @click="saveLog">保存</el-button>
        </template>
      </el-dialog>
    </template>
  </TabPage>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as echarts from 'echarts'
import TabPage from '../components/TabPage.vue'
import { api } from '../api'
import { todayStr } from '../utils/common'
import { axisStyle, tooltipStyle } from '../utils/echarts-theme'

const MEALS = { breakfast: '早餐', lunch: '午餐', dinner: '晚餐', snack: '加餐' }

const loading = ref(true)
const d = ref({})
const empty = computed(() => d.value.targets == null)
const logVisible = ref(false)
const saving = ref(false)
const logForm = ref({})
const aiEstimating = ref(false)
const balanceChart = ref(null)
const charts = []

const DAY_TYPES = { rest: '休息日', easy: '常规日', quality: '质量课', long: '长距离日' }

async function load() {
  loading.value = true
  try {
    d.value = await api.get('/diet')
    d.value.today = { ...(d.value.today || {}),
                      day_type_label: DAY_TYPES[d.value.today?.day_type] || '常规日' }
    loading.value = false
    await nextTick()
    renderBalance()
  } finally { loading.value = false }
}

function renderBalance() {
  const series = d.value.balance?.series || []
  if (!balanceChart.value || !series.length) return
  const chart = echarts.init(balanceChart.value)
  charts.push(chart)
  chart.setOption({
    grid: { left: 46, right: 46, top: 30, bottom: 26 },
    tooltip: { trigger: 'axis', ...tooltipStyle },
    legend: { textStyle: { color: '#8fada2' }, top: 0 },
    xAxis: { type: 'category', data: series.map(s => String(s.date).slice(5)), ...axisStyle },
    yAxis: [
      { type: 'value', ...axisStyle, splitLine: { lineStyle: { color: 'rgba(157,184,173,.08)' } } },
      { type: 'value', ...axisStyle, splitLine: { show: false } },
    ],
    series: [
      { name: '摄入−目标', type: 'bar', barWidth: '55%',
        data: series.map(s => ({ value: s.balance,
          itemStyle: { color: s.balance >= 0 ? 'rgba(95, 201, 135,.7)' : 'rgba(224,95,95,.8)', borderRadius: [3, 3, 0, 0] } })) },
      { name: '运动消耗 kcal', type: 'line', yAxisIndex: 1, smooth: true, symbol: 'none',
        data: series.map(s => s.burned_kcal), lineStyle: { color: '#d9a24e', width: 1.8 } },
    ],
  })
}

function openLog(row) {
  logForm.value = row
    ? { ...row }
    : { date: todayStr(), meal: 'lunch', description: '', kcal: 0, protein_g: 0, carb_g: 0, fat_g: 0 }
  logVisible.value = true
}

async function saveLog() {
  saving.value = true
  try {
    const body = { ...logForm.value }
    if (body.id) await api.put(`/diet/logs/${body.id}`, body)
    else await api.post('/diet/logs', body)
    ElMessage.success('已保存')
    logVisible.value = false
    await load()
  } catch (e) { ElMessage.error(e.message || '保存失败') } finally { saving.value = false }
}

async function removeLog(row) {
  await api.del(`/diet/logs/${row.id}`)
  await load()
}

async function askAiEstimate() {
  const { value } = await ElMessageBox.prompt('描述这餐吃了什么，AI 按常见份量估算营养素', 'AI 估算一餐',
    { inputPlaceholder: '如：牛肉面一碗 + 一个煎蛋', inputValue: '' })
  if (!value) return
  aiEstimating.value = true
  try {
    const est = await api.post('/diet/estimate', { description: value })
    logForm.value = { date: todayStr(), meal: 'lunch', description: value,
                      kcal: est.kcal ?? 0, protein_g: est.protein_g ?? 0,
                      carb_g: est.carb_g ?? 0, fat_g: est.fat_g ?? 0 }
    logVisible.value = true
  } catch (e) { ElMessage.error(e.message || '本地模型不可用，可手动填写') } finally { aiEstimating.value = false }
}

onMounted(load)
onUnmounted(() => charts.forEach(c => c.dispose()))
</script>

<style scoped>
.card-head { display: flex; align-items: baseline; gap: 10px; }
.card-sub { font-size: 12px; color: var(--text-3); font-weight: 400; }
.target-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; text-align: center; }
.tg-v { font-size: 24px; font-weight: 700; color: var(--text); }
.tg-k { font-size: 11px; color: var(--text-3); margin-top: 2px; }
</style>
