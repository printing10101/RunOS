<template>
  <el-card shadow="never">
    <template #header><div class="card-head">今日状态打卡 <span class="card-sub">{{ checkin.checkin ? '已打卡' : '晨间 10 秒' }}</span></div></template>
    <template v-if="!checkin.checkin">
      <div v-for="q in CHECKIN_QUESTIONS" :key="q.key" class="ck-row">
        <span class="ck-label">{{ q.label }}</span>
        <el-rate v-model="checkinForm[q.key]" :max="q.max" size="small"
                 :colors="['#ff6b6b', '#ffa24d', '#4ade80']" style="--el-rate-icon-margin: 2px" />
      </div>
      <el-input v-model="checkinPain" size="small" placeholder="疼痛/不适部位（可空）" style="margin-top:6px" />
      <el-button type="primary" size="small" round style="width:100%; margin-top:10px" @click="saveCheckin">提交打卡</el-button>
    </template>
    <template v-else>
      <div class="ck-advice" :style="{ borderColor: adviceColor }">
        <div class="ck-score num-display" :style="{ color: adviceColor }"><RollNum :value="checkin.advice?.score" /></div>
        <div class="ck-advice-body">
          <div class="ck-level">{{ adviceLabel }}
            <span v-if="fuelingTag" class="ck-fueling" :style="{ color: fuelingTag.color, borderColor: fuelingTag.color }">{{ fuelingTag.label }}</span>
          </div>
          <div class="ck-verdict">{{ checkin.advice?.verdict }}</div>
        </div>
      </div>
      <el-button size="small" text style="margin-top:6px" @click="resetCheckin">重新填写</el-button>
    </template>
  </el-card>
</template>

<script setup>
// 今日状态打卡卡片：问卷 + 建议档位展示，自含状态；父组件经 ref 调 loadCheckin() 刷新
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import RollNum from './RollNum.vue'
import { todayStr } from '../utils/common'

const checkin = ref({})
// 打卡问卷项（key 必须与后端打卡 schema 的字段名一致）。
// 模板必须遍历这个数组、而不是遍历 checkinForm 本身：遍历对象拿到的是数值 value，
// 会让 q.key / q.label / q.max 全为 undefined，四项标签与评分上限都渲染不出来。
const CHECKIN_QUESTIONS = [
  { key: 'sleep_quality', label: '睡眠质量', max: 5 },
  { key: 'muscle_soreness', label: '肌肉酸痛', max: 5 },
  { key: 'energy_level', label: '精力水平', max: 5 },
  { key: 'motivation', label: '训练动力', max: 5 },
]
const checkinForm = ref({ sleep_quality: 3, muscle_soreness: 0, energy_level: 3, motivation: 3 })
const checkinPain = ref('')

const ADVICE_LABELS = { normal: '按计划执行', reduce: '建议减量', easy: '仅轻松跑', rest: '今日休息' }
const adviceLabel = computed(() => ADVICE_LABELS[checkin.value.advice?.level] || '')
// 补给状态徽标：能量亏缺由饮食联动判定（fueling 来自后端 checkin 建议）
const fuelingTag = computed(() => {
  const lv = checkin.value.advice?.fueling_level
  if (lv === 'deficit') return { label: '能量缺口', color: '#ff6b6b' }
  if (lv === 'low') return { label: '补给偏低', color: '#ffa24d' }
  return null
})
const adviceColor = computed(() => {
  const lv = checkin.value.advice?.level
  return { normal: '#4ade80', reduce: '#ffa24d', easy: '#ffa24d', rest: '#ff6b6b' }[lv] || '#e8eef6'
})

async function loadCheckin() {
  try { checkin.value = await api.get('/checkin/today') } catch { /* ignore */ }
}

async function saveCheckin() {
  try {
    const today = todayStr()
    const r = await api.post('/checkin', {
      date: today,
      sleep_quality: checkinForm.value.sleep_quality,
      muscle_soreness: checkinForm.value.muscle_soreness,
      energy_level: checkinForm.value.energy_level,
      motivation: checkinForm.value.motivation,
      pain_area: checkinPain.value || '',
    })
    checkin.value = { ...(checkin.value.checkin ? checkin.value : {}), checkin: { date: today, ...checkinForm.value, pain_area: checkinPain.value || '' }, advice: r.advice }
    ElMessage.success(r.advice?.verdict || '已打卡')
  } catch (e) { ElMessage.error(e.message || '打卡失败') }
}

function resetCheckin() {
  const c = checkin.value.checkin
  if (c) {   // 重新填写时带入已保存的值，而不是回到默认
    checkinForm.value = { sleep_quality: c.sleep_quality, muscle_soreness: c.muscle_soreness,
                          energy_level: c.energy_level, motivation: c.motivation }
    checkinPain.value = c.pain_area || ''
  }
  checkin.value = { ...checkin.value, checkin: null }
}

defineExpose({ loadCheckin })
</script>

<style scoped>
.ck-row { display: flex; align-items: center; justify-content: space-between; padding: 3px 0; }
.ck-label { font-size: 12.5px; color: var(--text-2); }
.ck-advice { display: flex; gap: 12px; align-items: center; border: 1px solid var(--border); border-left: 3px solid var(--border);
  border-radius: 10px; padding: 10px 12px; background: var(--bg-inset); }
.ck-score { font-size: 32px; }
.ck-level { font-weight: 800; font-size: 13.5px; }
.ck-fueling { font-weight: 600; font-size: 11px; border: 1px solid; border-radius: 10px; padding: 0 7px; margin-left: 6px; }
.ck-verdict { font-size: 11.5px; color: var(--text-2); line-height: 1.6; margin-top: 3px; }
</style>
