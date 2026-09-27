<template>
  <el-skeleton v-if="loading" style="margin-top:8px" :rows="9" animated />
  <el-empty v-else-if="loadError" :description="loadError">
    <el-button type="primary" round @click="loadAll">重试</el-button>
  </el-empty>
  <div v-else-if="!d.empty">
    <h1 class="page-title">数据报表</h1>
    <div class="page-sub">月度 / 年度运动报告 · 连续打卡 · 个人纪录</div>

    <el-row :gutter="14">
      <el-col :span="8">
        <el-card shadow="never" class="sum-card">
          <template #header><div class="card-head">本月运动报告</div></template>
          <div class="sum-hero num-display"><RollNum :value="d.this_month?.km" fallback="" /><span class="sum-unit">km</span></div>
          <div class="sum-grid">
            <div><b class="num-display"><RollNum :value="d.this_month?.sessions" fallback="" /></b><span>次</span></div>
            <div><b class="num-display"><RollNum :value="d.this_month?.hours" fallback="" /></b><span>小时</span></div>
            <div><b class="num-display"><RollNum :value="d.this_month?.elev_m" fallback="" /></b><span>爬升 m</span></div>
            <div><b class="num-display"><RollNum :value="d.this_month?.calories" fallback="" /></b><span>千卡</span></div>
          </div>
          <div class="sum-foot" v-if="d.this_month?.avg_pace_sec">跑步均配速 {{ fmtPace(d.this_month.avg_pace_sec) }} · 最长 {{ d.this_month.longest_km }} km</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" class="sum-card">
          <template #header><div class="card-head">年度运动报告</div></template>
          <div class="sum-hero num-display"><RollNum :value="d.this_year?.km" fallback="" /><span class="sum-unit">km</span></div>
          <div class="sum-grid">
            <div><b class="num-display"><RollNum :value="d.this_year?.sessions" fallback="" /></b><span>次</span></div>
            <div><b class="num-display"><RollNum :value="d.this_year?.hours" fallback="" /></b><span>小时</span></div>
            <div><b class="num-display"><RollNum :value="d.this_year?.elev_m" fallback="" /></b><span>爬升 m</span></div>
            <div><b class="num-display"><RollNum :value="d.this_year?.calories" fallback="" /></b><span>千卡</span></div>
          </div>
          <div class="sum-foot" v-if="d.this_year?.avg_pace_sec">跑步均配速 {{ fmtPace(d.this_year.avg_pace_sec) }} · 最长 {{ d.this_year.longest_km }} km</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" class="sum-card">
          <template #header><div class="card-head">连续打卡</div></template>
          <div class="streak-row">
            <div class="streak-box">
              <div class="num-display streak-num" style="color: var(--jade)"><RollNum :value="d.streak?.current" fallback="" /></div>
              <div class="m-label">当前连续 · 天</div>
            </div>
            <div class="streak-box">
              <div class="num-display streak-num" style="color: var(--gold)"><RollNum :value="d.streak?.longest" fallback="" /></div>
              <div class="m-label">历史最长 · 天</div>
            </div>
          </div>
          <div class="highlights">
            <div v-for="(h, i) in d.highlights" :key="i" class="hl-row">{{ h.icon }} {{ h.text }}</div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="14" style="margin-top:14px">
      <el-col :span="14">
        <el-card shadow="never">
          <template #header><div class="card-head">跑量日历 <span class="card-sub">颜色越亮 · 跑量越大</span></div></template>
          <div ref="calChart" style="height: 260px"></div>
        </el-card>
      </el-col>
      <el-col :span="10">
        <el-card shadow="never">
          <template #header><div class="card-head">近 12 个月跑量</div></template>
          <div ref="monthChart" style="height: 260px"></div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" style="margin-top:14px">
      <template #header>
        <div class="card-head">个人纪录 PB
          <span class="card-sub">仅基于实测数据（设备分段滑窗 / 同距离整程实测）· 点击行查看该次活动</span>
        </div>
      </template>
      <el-table :data="pb.items" size="middle" @row-click="row => $router.push(`/activities/${row.activity_id}`)"
                class="click-table" v-if="pb.items?.length">
        <el-table-column prop="label" label="距离" width="110" />
        <el-table-column label="PB" width="110">
          <template #default="{ row }"><span class="pb-time num-display">{{ row.time_str }}</span></template>
        </el-table-column>
        <el-table-column label="配速" width="110" align="right">
          <template #default="{ row }">{{ fmtPace(row.pace_sec_per_km) }}</template>
        </el-table-column>
        <el-table-column label="依据" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="row.basis === 'window' ? 'success' : 'info'" effect="plain">
              {{ row.basis === 'window' ? '设备分段滑窗' : '整程实测' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="activity_title" label="来自活动" min-width="200" show-overflow-tooltip />
        <el-table-column label="日期" width="110" align="right">
          <template #default="{ row }">{{ row.date }}</template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="暂无足够跑步数据计算 PB" :image-size="60" />
      <div v-if="pb.note" class="pb-note">{{ pb.note }}</div>
    </el-card>
  </div>
  <el-empty v-else description="请先完善档案并录入训练数据" />
</template>

<script setup>
import { localDateStr } from '../utils/common'
import RollNum from '../components/RollNum.vue'
import { onMounted, onUnmounted, ref, nextTick } from 'vue'
import * as echarts from 'echarts'
import { api, fmtPace } from '../api'
import { axisStyle, tooltipStyle } from '../utils/echarts-theme'
import { createChartManager } from '../composables/charts'

const d = ref({ empty: true })
const pb = ref({})
const loadError = ref('')
const loading = ref(true)
const calChart = ref(null)
const monthChart = ref(null)
// 统一管理图表实例：init 去重、窗口缩放自动 resize、卸载时释放
const charts = createChartManager()

async function loadAll() {
  loading.value = true
  loadError.value = ''
  try {
    d.value = await api.get('/stats/report')
    if (d.value.empty) return
    pb.value = await api.get('/stats/pb')
    // 先撤骨架屏（图表容器在 v-else 分支里，不撤就永远拿不到 ref），等 DOM 挂载后再画图
    loading.value = false
    await nextTick()
    await renderCharts()
  } catch (e) {
    loadError.value = e.message || '加载失败，请确认后端服务正在运行'
  } finally { loading.value = false }
}

async function renderCharts() {
  // 日历热力图（近 12 个月）
  const cal = await api.get('/stats/calendar', { days: 365 })
  if (calChart.value && cal.items?.length) {
    const data = cal.items.filter(x => x.km > 0).map(x => [x.date, x.km])
    const end = new Date()
    const start = new Date(end.getTime() - 364 * 86400000)
    charts.get('cal', calChart.value).setOption({
      tooltip: { ...tooltipStyle, formatter: p => `${p.value[0]}<br/>跑量 ${p.value[1]} km` },
      visualMap: { show: false, min: 0, max: Math.max(12, ...cal.items.map(x => x.km)),
        inRange: { color: ['rgba(63,208,164,.08)', 'rgba(63,208,164,.35)', '#3fd0a4'] } },
      calendar: {
        top: 30, left: 44, right: 12, cellSize: ['auto', 15],
        range: [localDateStr(start), localDateStr(end)],
        splitLine: { lineStyle: { color: 'rgba(157,184,173,.2)' } },
        yearLabel: { show: false },
        monthLabel: { color: '#5c6f68', fontSize: 10 },
        dayLabel: { color: '#5c6f68', fontSize: 9, nameMap: ['日', '一', '二', '三', '四', '五', '六'] },
        itemStyle: { color: 'rgba(157,184,173,.05)', borderColor: '#081210', borderWidth: 2 },
      },
      series: [{ type: 'heatmap', coordinateSystem: 'calendar', data }],
    })
  }

  // 月度柱图
  if (monthChart.value) {
    const months = d.value.monthly || []
    charts.get('month', monthChart.value).setOption({
      grid: { left: 42, right: 10, top: 24, bottom: 26 },
      tooltip: { trigger: 'axis', ...tooltipStyle },
      xAxis: { type: 'category', data: months.map(m => m.month.slice(5)), ...axisStyle },
      yAxis: { type: 'value', ...axisStyle, splitLine: { lineStyle: { color: 'rgba(157,184,173,.08)' } } },
      series: [{ type: 'bar', data: months.map(m => m.km), barWidth: '55%',
        itemStyle: { borderRadius: [4, 4, 0, 0],
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: '#3fd0a4' }, { offset: 1, color: 'rgba(63,208,164,.2)' }]) } }],
    })
  }
}

onMounted(loadAll)
onUnmounted(charts.disposeAll)
</script>

<style scoped>
.sum-card { text-align: center; }
.sum-hero { font-size: 44px; color: var(--text); margin: 4px 0 10px; }
.sum-unit { font-size: 15px; color: var(--text-3); margin-left: 4px; }
.sum-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin-bottom: 10px; }
.sum-grid b { display: block; font-size: 19px; color: var(--text); font-family: var(--font-display); }
.sum-grid span { font-size: 10.5px; color: var(--text-3); }
.sum-foot { font-size: 11.5px; color: var(--text-2); }
.streak-row { display: flex; justify-content: space-around; margin: 6px 0 10px; }
.streak-num { font-size: 40px; }
.m-label { font-size: 11px; color: var(--text-3); margin-top: 4px; }
.highlights { text-align: left; }
.hl-row { font-size: 12px; color: var(--text-2); padding: 4px 0; border-top: 1px dashed rgba(157,184,173,.1); }
.click-table :deep(.el-table__row) { cursor: pointer; }
.pb-time { font-size: 17px; color: var(--jade); }
.pb-note { font-size: 11.5px; color: var(--text-3); margin-top: 10px; }
</style>
