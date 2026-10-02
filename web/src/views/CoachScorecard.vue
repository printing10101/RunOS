<template>
  <div v-loading="loading">
    <el-empty v-if="!loading && s?.empty"
              description="还没有提案记录：引擎与 AI 教练的调整建议在采纳执行后，会在这里对答案" />
    <template v-else-if="s">
      <el-alert :title="s.headline" type="info" :closable="false" show-icon style="margin-bottom:14px" />

      <el-row :gutter="14">
        <el-col :span="6">
          <el-card shadow="never">
            <template #header>提案采纳率</template>
            <div class="hero-num">{{ pct(s.proposals?.acceptance_rate) }}</div>
            <div class="hero-sub">
              {{ s.proposals?.applied ?? 0 }} 采纳 / {{ s.proposals?.dismissed ?? 0 }} 忽略
              （自动撤回 {{ s.proposals?.withdrawn ?? 0 }} 条不算用户的决定）
            </div>
          </el-card>
        </el-col>
        <el-col :span="6">
          <el-card shadow="never">
            <template #header>采纳后执行落地</template>
            <div class="hero-num">{{ pct(s.execution?.landed_rate) }}</div>
            <div class="hero-sub">
              {{ s.execution?.landed ?? 0 }}/{{ s.execution?.tracked ?? 0 }} 节课已落地
              <template v-if="s.execution?.pending_execution"> · {{ s.execution.pending_execution }} 节待执行</template>
            </div>
          </el-card>
        </el-col>
        <el-col :span="12">
          <el-card shadow="never">
            <template #header>
              按提案类型
              <span class="card-sub">对话提案卡发出 {{ s.proposals?.chat_emitted ?? 0 }} 张（apply 不落卡，不计入采纳率）</span>
            </template>
            <el-table :data="s.proposals?.by_kind || []" size="small">
              <el-table-column prop="kind" label="类型" min-width="130" />
              <el-table-column prop="emitted" label="发出" width="62" />
              <el-table-column prop="applied" label="采纳" width="62" />
              <el-table-column prop="dismissed" label="忽略" width="62" />
              <el-table-column label="采纳率" width="80">
                <template #default="{ row }">{{ pct(row.acceptance_rate) }}</template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-col>
      </el-row>

      <el-card shadow="never" style="margin-top:14px">
        <template #header>
          近 8 周：课表执行率 × 当周建议决定
          <span class="card-sub">两组数字并排只作对照，不构成因果结论</span>
        </template>
        <el-empty v-if="!(s.weeks || []).length" description="还没有计划课执行记录" :image-size="60" />
        <el-table v-else :data="s.weeks" size="small">
          <el-table-column label="周（周一）" width="110">
            <template #default="{ row }">{{ row.week_start?.slice(5) }}</template>
          </el-table-column>
          <el-table-column label="到期课完成" width="100">
            <template #default="{ row }">{{ row.completed }}/{{ row.planned }}</template>
          </el-table-column>
          <el-table-column label="执行率" min-width="160">
            <template #default="{ row }">
              <el-progress :percentage="Math.round((row.execution_rate || 0) * 100)"
                           :stroke-width="8" :show-text="false" color="#3fd0a4" />
            </template>
          </el-table-column>
          <el-table-column prop="applied" label="当周采纳建议" width="110" />
          <el-table-column prop="dismissed" label="当周忽略建议" width="110" />
        </el-table>
      </el-card>
    </template>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const s = ref(null)
const loading = ref(true)

function pct(rate) {
  return rate == null ? '—' : `${Math.round(rate * 100)}%`
}

onMounted(async () => {
  try {
    s.value = await api.get('/ai/coach-scorecard')
  } finally { loading.value = false }
})
</script>

<style scoped>
.hero-num { font-size: 34px; font-weight: 800; color: var(--jade); line-height: 1.2; }
.hero-sub { font-size: 12.5px; color: var(--text-3); margin-top: 6px; line-height: 1.6; }
.card-sub { font-weight: 400; font-size: 11.5px; color: var(--text-3); margin-left: 8px; }
</style>
