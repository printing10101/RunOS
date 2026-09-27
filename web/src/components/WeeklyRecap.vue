<template>
  <el-alert v-if="recapError" type="info" title="本周复盘暂时加载失败，其余数据不受影响" :closable="false"
            style="margin-top:14px" show-icon />
  <el-card shadow="never" style="margin-top:14px" v-if="recap && !recap.empty">
    <template #header>
      <div class="card-head">本周复盘 <span class="card-sub">{{ recap.week?.range }} · 每周自动 · {{ weekPhaseName }}</span>
        <el-button size="small" text bg class="recap-ai-btn" :loading="recapReviewLoading"
                   @click="loadReview">AI 点评</el-button>
      </div>
    </template>
    <div class="recap-lead">📋 {{ recap.lead }}</div>
    <div v-if="recapReview" class="recap-review">
      <el-tag size="small" :type="recapReviewSource === 'llm' ? 'success' : 'info'">
        {{ recapReviewSource === 'llm' ? 'AI 点评' : '规则点评（本地模型不可用）' }}
      </el-tag>
      <div class="recap-review-text">{{ recapReview }}</div>
    </div>
    <el-row :gutter="14" class="recap-stats">
      <el-col :span="4"><div class="rs-num num-display"><RollNum :value="recap.this_week?.sessions" /></div><div class="rs-label">训练次数</div></el-col>
      <el-col :span="4"><div class="rs-num num-display"><RollNum :value="recap.this_week?.km" /></div><div class="rs-label">本周公里</div></el-col>
      <el-col :span="4"><div class="rs-num num-display"><RollNum :value="recap.split?.this?.hard_pct" fallback="0" />%</div><div class="rs-label">强度占比</div></el-col>
      <el-col :span="4"><div class="rs-num num-display" :style="{ color: acwrTone }"><RollNum :value="recap.load?.acwr" /></div><div class="rs-label">ACWR</div></el-col>
      <el-col :span="4"><div class="rs-num num-display" :style="{ color: recap.recovery?.readiness >= 60 ? 'var(--green)' : 'var(--red)' }">
        <RollNum :value="recap.recovery?.readiness" /></div><div class="rs-label">准备度</div></el-col>
      <el-col :span="4"><div class="rs-num num-display"><RollNum :value="recap.recovery?.recovery_h" fallback="0" /><span class="rs-unit">h</span></div><div class="rs-label">恢复剩余</div></el-col>
    </el-row>
    <div class="recap-status" v-if="recap.recovery">{{ recap.recovery.status }} · {{ recap.recovery.status_detail }}</div>
    <div class="recap-next" v-if="recap.next">
      <b class="recap-next-title">下周 · 第 {{ recap.next.week_index }} 周 · {{ phaseName(recap.next.phase) }} <span class="recap-target">目标 {{ recap.next.target_km }} km</span></b>
      <div class="recap-wos" v-if="recap.next.titles?.length">
        <span v-for="w in recap.next.titles" :key="w.date" class="rc-woc">{{ w.title }}</span>
      </div>
    </div>
  </el-card>
</template>

<script setup>
// 每周自动复盘卡片：跑后环节的本周数据 + 下周预览；父组件经 ref 调 loadRecap() 刷新
import { computed, ref } from 'vue'
import { api } from '../api'
import RollNum from './RollNum.vue'
import { phaseName, acwrColor } from '../utils/common'
import { useAiReview } from '../composables/aiReview'

const recap = ref({})
const recapError = ref(false)
// 周复盘 AI 点评与其它解读页共用同一组合式（端点契约一致：{review, source}）
const { review: recapReview, reviewSource: recapReviewSource,
        reviewLoading: recapReviewLoading, loadReview } =
  useAiReview('/ai/weekly-recap-review', { failTip: 'AI 点评生成失败' })

const weekPhaseName = computed(() => phaseName(recap.value.next?.phase) || '未安排')
// ACWR 配色统一走 utils/acwrColor（阈值是训练学安全线，勿在视图里内联）
const acwrTone = computed(() => acwrColor(recap.value.load?.acwr, 'var(--text-3)'))

function loadRecap() {
  api.get('/dashboard/recap-weekly').then(r => {
    recapError.value = false
    if (!r.empty) recap.value = r
  }).catch(() => { recapError.value = true })
}

defineExpose({ loadRecap })
</script>

<style scoped>
.recap-lead { font-size: 13.5px; color: var(--text-2); line-height: 1.7; padding: 2px 2px 12px; }
.recap-ai-btn { margin-left: auto; align-self: center; }
.recap-review { margin: 0 2px 12px; padding: 10px 12px; background: var(--bg-inset);
  border: 1px solid var(--border); border-radius: 8px; }
.recap-review-text { white-space: pre-line; font-size: 13.5px; line-height: 1.8; color: var(--text-1); margin-top: 8px; }
.recap-stats { text-align: center; }
.recap-stats .el-col { padding: 10px 0; border: 1px solid var(--border); border-radius: 10px; background: var(--bg-inset); margin-bottom: 8px; }
.rs-num { font-size: 24px; color: var(--text); }
.rs-unit { font-size: 11px; color: var(--text-3); margin-left: 2px; }
.rs-label { font-size: 11px; color: var(--text-3); margin-top: 2px; letter-spacing: .04em; }
.recap-status { font-size: 12px; color: var(--text-3); margin-top: 14px; line-height: 1.6; }
.recap-next { margin-top: 8px; border-top: 1px dashed rgba(157,184,173,.14); padding-top: 12px; }
.recap-next-title { font-size: 13.5px; }
.recap-target { font-weight: 400; color: var(--text-3); margin-left: 6px; }
.recap-wos { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.rc-woc { font-size: 12px; color: var(--text-2); background: rgba(157,184,173,.1); padding: 4px 10px; border-radius: 8px; }
</style>
