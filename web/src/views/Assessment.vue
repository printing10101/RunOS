<template>
  <div v-loading="loading">
    <div style="display:flex; justify-content:flex-end; margin-bottom:10px">
      <el-button type="primary" :loading="computing" @click="compute">重新评估</el-button>
    </div>

    <el-alert v-if="a.runner_type && a.runner_type.event !== 'unknown'" type="info" :closable="false" class="rt-banner">
      <template #title>
        <span style="font-weight:700">🏃 {{ a.runner_type.headline }}</span>
        <el-tag size="small" style="margin:0 8px">{{ a.runner_type.level }}</el-tag>
        <el-tag size="small" :type="a.runner_type.style === 'competitive' ? 'warning' : 'success'">{{ a.runner_type.style_name }}</el-tag>
        <span style="font-weight:400; margin-left:10px; font-size:13px">{{ a.runner_type.description }}</span>
      </template>
    </el-alert>

    <el-row :gutter="14">
      <el-col :span="9">
        <el-card shadow="never">
          <template #header>五维画像</template>
          <div ref="radar" style="height: 300px"></div>
          <div class="score-line" v-if="a.total_score">
            <div class="grade-badge" :style="{ background: gradeColor(a.grade) }">{{ a.grade }}</div>
            <div>
              <div class="stat-num">{{ a.total_score }} <span style="font-size:14px; color:var(--text-3)">/ 100</span></div>
              <div class="stat-label">总体得分 · 超过同龄同性别 {{ a.percentile }}% 的跑者</div>
            </div>
          </div>
        </el-card>
        <el-card shadow="never" style="margin-top:14px">
          <template #header>强度分布（近 6 个月）</template>
          <template v-if="a.intensity_distribution?.easy_pct != null">
            <el-progress :percentage="a.intensity_distribution.easy_pct" color="#5fc987" :format="() => '轻松 ' + a.intensity_distribution.easy_pct + '%'" />
            <el-progress :percentage="a.intensity_distribution.moderate_pct" color="#d9a24e" :format="() => '中等 ' + a.intensity_distribution.moderate_pct + '%'" />
            <el-progress :percentage="a.intensity_distribution.hard_pct" color="#e05f5f" :format="() => '高强度 ' + a.intensity_distribution.hard_pct + '%'" />
            <el-alert :title="a.intensity_distribution.verdict" :type="a.intensity_distribution.easy_pct >= 70 && a.intensity_distribution.hard_pct <= 25 ? 'success' : 'warning'"
                      :closable="false" show-icon style="margin-top:10px" />
          </template>
          <el-empty v-else description="数据不足" :image-size="50" />
        </el-card>
      </el-col>

      <el-col :span="15">
        <el-card shadow="never">
          <template #header>维度明细</template>
          <el-collapse v-model="openDims">
            <el-collapse-item v-for="(dim, key) in a.dimensions" :key="key" :name="key">
              <template #title>
                <div style="display:flex; align-items:center; gap:10px; width:100%">
                  <b style="width:90px">{{ a.dimension_labels[key] }}</b>
                  <el-progress :percentage="dim.score || 0" :stroke-width="10"
                               :color="dimColor(dim.score)" style="flex:1" />
                  <b style="width:36px; text-align:right">{{ dim.score ?? '-' }}</b>
                  <span class="dim-weight">{{ Math.round(a.dimension_weights[key] * 100) }}%</span>
                </div>
              </template>
              <ul class="evidence">
                <li v-for="e in dim.evidence" :key="e">{{ e }}</li>
              </ul>
              <!-- 缺数据的项不会拿「中等」值顶上，而是明确列出来，并把权重分给可用项 -->
              <el-alert v-if="dim.gaps?.length" type="warning" :closable="false" show-icon
                        :title="'这些数据还没有，已从本项总分中剔除（不按中等值顶替）：' + dim.gaps.join('；')"
                        style="margin-top:6px" />
              <template v-if="key === 'talent'">
                <el-alert v-if="dim.label" :title="`天赋评价：${dim.label}（基础起点 ${dim.base_score} / 训练响应 ${dim.response_score ?? '数据不足'}）`"
                          type="info" :closable="false" show-icon />
              </template>
              <template v-if="key === 'strength'">
                <div style="font-size:13px; color:var(--text-2); margin-top:6px">{{ dim.summary }}</div>
              </template>
            </el-collapse-item>
          </el-collapse>
        </el-card>

        <el-card shadow="never" style="margin-top:14px">
          <template #header>
            <div style="display:flex; align-items:center">
              <span>短板诊断与改进建议</span>
              <span style="margin-left:auto; font-size:12px; color:var(--text-3)">
                <el-tag size="small" type="warning" style="margin-right:6px">可训练</el-tag>后天改进
                <el-tag size="small" type="info" style="margin-left:10px; margin-right:6px">先天特质</el-tag>应对与扬长
              </span>
            </div>
          </template>
          <div v-if="a.weakness?.goal_gap" class="goal-gap">
            <b>{{ a.weakness.goal_gap.goal }}</b>
            <span style="margin-left:10px; color:var(--text-2); font-size:13px">
              需要 VDOT {{ a.weakness.goal_gap.required_vdot }} · 当前 {{ a.weakness.goal_gap.current_vdot }}
              （差距 {{ a.weakness.goal_gap.gap }}）
            </span>
            <div style="font-size:13px; color:#5f9fc9; margin-top:4px">{{ a.weakness.goal_gap.verdict }}</div>
          </div>
          <el-collapse>
            <el-collapse-item v-for="f in a.weakness?.findings || []" :key="f.id">
              <template #title>
                <div style="display:flex; align-items:center; gap:8px">
                  <el-tag size="small" :type="f.category === 'trainable' ? 'warning' : 'info'">
                    {{ f.category === 'trainable' ? '可训练' : '先天' }}
                  </el-tag>
                  <el-tag v-if="f.severity === 'high'" size="small" type="danger">高优先</el-tag>
                  <span style="font-weight:600">{{ f.title }}</span>
                </div>
              </template>
              <div class="finding-body">
                <p><b>是什么：</b>{{ f.what }}</p>
                <p><b>为什么：</b>{{ f.why }}</p>
                <p><b>影响：</b>{{ f.impact }}</p>
                <p style="margin-bottom:4px"><b>{{ f.category === 'trainable' ? '如何改进' : '应对策略' }}：</b></p>
                <ul class="evidence">
                  <li v-for="(act, i) in f.actions" :key="i">{{ act }}</li>
                </ul>
              </div>
            </el-collapse-item>
          </el-collapse>
          <div style="margin-top:12px; padding:10px 12px; background:var(--bg-inset); border:1px solid var(--border); border-radius:8px; font-size:13px; color:var(--text-2)">
            💡 {{ a.weakness?.summary }}
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { gradeColor } from '../utils/common'
import { createChartManager } from '../composables/charts'

const a = ref({})
const radar = ref(null)
const loading = ref(true)
const charts = createChartManager()
const computing = ref(false)
const openDims = ref(['aerobic', 'willpower', 'talent'])

function dimColor(s) {
  return s >= 80 ? '#3fd0a4' : s >= 60 ? '#5f9fc9' : s >= 40 ? '#d9a24e' : '#e05f5f'
}

async function load() {
  loading.value = true
  try {
    a.value = await api.post('/assessment/compute', {})
    renderRadar()
  } finally { loading.value = false }
}

async function compute() {
  computing.value = true
  try { await load(); ElMessage.success('评估已更新') } finally { computing.value = false }
}

function renderRadar() {
  const labels = a.value.dimension_labels || {}
  const dims = a.value.dimensions || {}
  
  charts.get('radar', radar.value).setOption({
    radar: {
      indicator: Object.keys(labels).map(k => ({ name: labels[k], max: 100 })),
      radius: '68%',
      axisName: { color: '#8fada2', fontSize: 12 },
      splitArea: { areaStyle: { color: ['rgba(157,184,173,0.02)', 'rgba(157,184,173,0.05)'] } },
      splitLine: { lineStyle: { color: 'rgba(157,184,173,0.12)' } },
      axisLine: { lineStyle: { color: 'rgba(157,184,173,0.12)' } },
    },
    series: [{
      type: 'radar',
      data: [{
        value: Object.keys(labels).map(k => dims[k]?.score ?? 0),
        name: '综合画像',
        areaStyle: { color: 'rgba(63,208,164,0.18)' },
        lineStyle: { color: '#3fd0a4', width: 2.5 },
        itemStyle: { color: '#3fd0a4' },
        symbolSize: 5,
      }],
    }],
  })
}

onMounted(load)
onUnmounted(charts.disposeAll)
</script>

<style scoped>
.rt-banner { margin-bottom: 14px; }
.rt-banner :deep(.el-alert__title) { display: flex; align-items: center; flex-wrap: wrap; }
.score-line { display: flex; gap: 14px; align-items: center; padding: 8px 6px 0; }
.grade-badge {
  width: 56px; height: 56px; border-radius: 14px; color: #081210; font-size: 28px; font-weight: 800;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.evidence { margin: 4px 0 0; padding-left: 18px; color: var(--text-2); font-size: 13px; line-height: 1.9; }
.dim-weight { font-size: 11px; color: var(--text-3); width: 32px; text-align: right; }
.finding-body p { margin: 4px 0; font-size: 13px; color: var(--text-2); line-height: 1.7; }
.goal-gap { padding: 10px 14px; background: rgba(95, 159, 201, 0.08); border: 1px solid rgba(95, 159, 201, 0.2); border-radius: 8px; margin-bottom: 10px; }
</style>
