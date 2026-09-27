<template>
  <div>
    <div class="filter-bar">
      <el-radio-group v-model="fRace" size="small">
        <el-radio-button value="">全部项目</el-radio-button>
        <el-radio-button value="5k">5K</el-radio-button>
        <el-radio-button value="10k">10K</el-radio-button>
        <el-radio-button value="marathon">全马</el-radio-button>
      </el-radio-group>
      <el-select v-model="fLevel" size="small" placeholder="水平" clearable style="width:110px">
        <el-option label="入门" value="beginner" /><el-option label="进阶" value="intermediate" />
        <el-option label="高级" value="advanced" />
      </el-select>
      <span class="f-count">{{ filtered.length }} 个计划</span>
    </div>

    <el-row :gutter="14">
      <el-col :span="12" v-for="pl in filtered" :key="pl.code" style="margin-bottom:14px">
        <el-card shadow="never" class="pl-card">
          <div class="pl-head">
            <div>
              <b class="pl-name">{{ pl.name_zh }}</b>
              <div class="pl-author">{{ pl.author }}</div>
            </div>
            <el-tag size="small" :type="evTagType(pl.evidence_level)" effect="dark">{{ pl.evidence_level }}</el-tag>
          </div>
          <div class="pl-stats">
            <span>{{ pl.weeks }} 周</span><i>/</i>
            <span>峰值 {{ pl.weekly_km_range?.min }}-{{ pl.weekly_km_range?.max }} km/周</span><i>/</i>
            <span>每周 {{ pl.days_per_week?.min }}-{{ pl.days_per_week?.max }} 练</span><i>/</i>
            <span>{{ raceLabel(pl.target_race) }} · {{ levelLabel(pl.level) }}</span>
          </div>
          <div class="pl-fit" v-if="pl.fit_notes">🎯 {{ pl.fit_notes }}</div>

          <el-collapse class="pl-more">
            <el-collapse-item :title="'计划结构与出处'">
              <div class="pb-title">周模板</div>
              <div class="pb-body">{{ weekText(pl) || '按阶段弹性安排' }}</div>
              <div class="pb-title" style="margin-top:10px">长距离递进</div>
              <div class="pb-body" v-if="longRunText(pl)">{{ longRunText(pl) }}</div>
              <div class="pb-title" style="margin-top:10px">关键课</div>
              <ul class="pb-list"><li v-for="(k, i) in pl.key_workouts" :key="i">{{ k }}</li></ul>
              <div class="pb-title" style="margin-top:10px">减量方案</div>
              <div class="pb-body">{{ pl.taper_plan }}</div>
              <div class="pb-pc">
                <div class="pc-item pro"><b>优点</b>{{ pl.pros }}</div>
                <div class="pc-item con"><b>局限</b>{{ pl.cons }}</div>
                <div class="pc-item warn"><b>⚠️ 注意</b>{{ pl.cautions }}</div>
              </div>
              <div class="pb-title" style="margin-top:10px">证据边界</div>
              <div class="pb-body" style="color: var(--text-3)">{{ pl.evidence_note }}</div>
              <div class="pb-src" v-for="(s, i) in pl.sources" :key="i">
                <el-tag size="small" :type="evTagType(s.level)" effect="dark">{{ s.level }}</el-tag>
                <div>
                  <a v-if="s.url" :href="s.url" target="_blank" rel="noopener">{{ s.title }}</a>
                  <span v-else>{{ s.title }}</span>
                </div>
              </div>
            </el-collapse-item>
          </el-collapse>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../../api'

const items = ref([])
const fRace = ref('')
const fLevel = ref(null)

const RACE = { '5k': '5K', '10k': '10K', hm: '半马', marathon: '全马', any: '通用' }
const LEVEL = { beginner: '入门', intermediate: '进阶', advanced: '高级', elite: '精英' }
const WK = { mon: '一', tue: '二', wed: '三', thu: '四', fri: '五', sat: '六', sun: '日' }
const SESSION = {
  easy: '轻松跑', easy_short: '短轻松跑', recovery: '恢复跑', recovery_cross: '恢复/交叉',
  tempo: '节奏跑', interval: '间歇', long: '长距离', long_pace: '带配速长距离', long_with_mp: '马配长距离',
  medium_long: '中长距离', medium_long_or_LT: '中长/阈值', LT_or_VO2max: '阈值/VO2max',
  general_aerobic: '一般有氧', speed_sos: '速度SOS', tempo_or_mp_sos: '节奏/马配SOS',
  track_intervals: '跑道间歇', threshold_am: '阈值(早)', threshold_pm: '阈值(晚)',
  quality_1: '质量课Q1', quality_2: '质量课Q2', strength: '力量', race: '比赛', rest: '休息',
}
const raceLabel = v => RACE[v] || v
const levelLabel = v => LEVEL[v] || v
const evTagType = lv => ({ A: 'success', B: 'primary', C: 'warning', D: 'info' }[lv] || 'info')

function weekText(pl) {
  return Object.entries(pl.week_template || {})
    .filter(([k, v]) => WK[k] && SESSION[v])
    .map(([k, v]) => `周${WK[k]}·${SESSION[v]}`).join(' · ')
}
function longRunText(pl) {
  const lr = pl.long_run_progression || []
  return lr.map(x => `W${x.week} ${x.km}km${x.note ? ` ${x.note}` : ''}`).join(' → ')
}

const filtered = computed(() => items.value.filter(pl =>
  (!fRace.value || pl.target_race === fRace.value || pl.target_race === 'any')
  && (!fLevel.value || pl.level === fLevel.value)))

onMounted(async () => {
  const d = await api.get('/methods/plans')
  items.value = d.items
})
</script>

<style scoped>
.filter-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
.f-count { margin-left: auto; font-size: 12px; color: var(--text-3); }
.pl-card { height: 100%; }
.pl-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
.pl-name { font-size: 14px; line-height: 1.4; }
.pl-author { font-size: 11px; color: var(--text-3); margin-top: 3px; }
.pl-stats { font-size: 12px; color: var(--text-2); margin-top: 10px; }
.pl-stats i { color: var(--text-3); margin: 0 6px; font-style: normal; }
.pl-fit { font-size: 12px; line-height: 1.7; color: var(--jade-deep); margin-top: 8px; }
.pl-more { border-top: 1px dashed var(--border); margin-top: 6px; }
.pb-title { font-size: 11px; color: var(--text-3); margin-bottom: 4px; }
.pb-body { font-size: 12px; line-height: 1.8; color: var(--text-2); }
.pb-list { margin: 0; padding-left: 18px; font-size: 12px; line-height: 1.8; color: var(--text-2); }
.pb-pc { margin-top: 10px; }
.pc-item { border-radius: 8px; padding: 8px 10px; font-size: 12px; line-height: 1.7; margin-bottom: 6px;
  color: var(--text-2); background: var(--bg-inset); border: 1px solid var(--border); }
.pc-item b { display: block; font-size: 11px; margin-bottom: 3px; font-weight: 400; }
.pc-item.pro b { color: var(--green); }
.pc-item.con b { color: var(--orange); }
.pc-item.warn { border-color: rgba(217, 162, 78, .35); }
.pc-item.warn b { color: var(--orange); }
.pb-src { display: flex; gap: 8px; font-size: 12px; margin-top: 8px; align-items: flex-start; }
.pb-src a { color: var(--blue); text-decoration: none; line-height: 1.6; }
</style>
