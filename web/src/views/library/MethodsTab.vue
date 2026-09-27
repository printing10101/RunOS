<template>
  <div v-loading="loading">
    <!-- 过滤栏 -->
    <div class="filter-bar">
      <el-radio-group v-model="fOrigin" size="small">
        <el-radio-button value="">全部来源</el-radio-button>
        <el-radio-button value="japan">日本 / 亚洲</el-radio-button>
        <el-radio-button value="western">西方</el-radio-button>
        <el-radio-button value="global">全球</el-radio-button>
      </el-radio-group>
      <el-select v-model="fLevel" size="small" placeholder="水平" clearable style="width:110px">
        <el-option label="入门" value="beginner" /><el-option label="进阶" value="intermediate" />
        <el-option label="高级" value="advanced" /><el-option label="精英" value="elite" />
      </el-select>
      <el-input v-model="fQuery" size="small" placeholder="搜索：川内 / 驿传 / 阈值 / 间歇…" clearable style="width:220px" />
      <span class="f-count">{{ filtered.length }} 个体系</span>
    </div>

    <el-row :gutter="14">
      <el-col :span="8" v-for="m in filtered" :key="m.code" style="margin-bottom:14px">
        <el-card shadow="never" class="m-card" @click="openDetail(m.code)">
          <div class="m-head">
            <b class="m-name">{{ m.name_zh }}</b>
            <el-tag size="small" :type="evTagType(m.evidence_level)" effect="dark">{{ m.evidence_level }}</el-tag>
          </div>
          <div class="m-sub" v-if="m.name_ja || m.name_en">{{ m.name_ja || m.name_en }}</div>
          <div class="m-fit">
            <span>亚洲适配</span>
            <el-progress :percentage="m.asian_fit" :stroke-width="6" :show-text="false"
                         :color="m.asian_fit >= 80 ? '#4ade80' : m.asian_fit >= 65 ? '#ffa24d' : '#5f6d80'"
                         style="flex:1" />
            <b>{{ m.asian_fit }}</b>
          </div>
          <div class="m-dist">
            <span class="md-seg" :style="{ width: (m.intensity_distribution?.low || 0) + '%', background: 'var(--green)' }"></span>
            <span class="md-seg" :style="{ width: (m.intensity_distribution?.moderate || 0) + '%', background: 'var(--orange)' }"></span>
            <span class="md-seg" :style="{ width: (m.intensity_distribution?.high || 0) + '%', background: 'var(--red)' }"></span>
          </div>
          <div class="m-meta">
            {{ kmRange(m) }} · 每周 {{ m.quality_days_per_week }} 次质量课 · {{ levelLabel(m.level) }}
          </div>
          <div class="m-tags">
            <el-tag v-for="t in (m.tags || []).slice(0, 4)" :key="t" size="small" effect="plain">{{ t }}</el-tag>
          </div>
        </el-card>
      </el-col>
    </el-row>
    <el-empty v-if="!filtered.length" description="没有匹配的训练方法，换个条件试试" />

    <!-- 详情抽屉 -->
    <el-drawer v-model="drawer" size="46%" :title="detail?.name_zh || '方法详情'">
      <div v-if="detail" class="detail">
        <div class="d-sub">{{ [detail.name_ja, detail.name_en].filter(Boolean).join(' · ') }}</div>
        <div class="d-tags">
          <el-tag size="small" :type="evTagType(detail.evidence_level)" effect="dark">证据 {{ detail.evidence_level }}</el-tag>
          <el-tag size="small" effect="plain">{{ originLabel(detail.origin) }}</el-tag>
          <el-tag v-for="t in detail.tags" :key="t" size="small" effect="plain">{{ t }}</el-tag>
        </div>
        <p class="d-summary">{{ detail.summary }}</p>

        <h4>核心原则</h4>
        <ul class="d-list"><li v-for="(p, i) in detail.principles" :key="i">{{ p }}</li></ul>

        <div class="d-pc">
          <div class="d-pc-item pro"><div class="pc-t">优点</div>{{ detail.pros }}</div>
          <div class="d-pc-item con"><div class="pc-t">局限</div>{{ detail.cons }}</div>
          <div class="d-pc-item warn" v-if="detail.cautions"><div class="pc-t">⚠️ 注意</div>{{ detail.cautions }}</div>
        </div>

        <div class="d-basis" v-if="detail.asian_fit_basis">
          <h4>亚洲适配依据（{{ detail.asian_fit }}/100）</h4>
          <div class="basis-row" v-for="(v, k) in detail.asian_fit_basis" :key="k">
            <span class="b-k">{{ basisLabel(k) }}</span><span class="b-v">{{ v }}</span>
          </div>
        </div>

        <h4>课表模板（{{ detail.workouts?.length || 0 }}）</h4>
        <el-collapse class="wk-list">
          <el-collapse-item v-for="w in detail.workouts" :key="w.code" :name="w.code">
            <template #title>
              <span class="wk-title">{{ w.name_zh }}</span>
              <el-tag size="small" effect="plain" style="margin-left:8px">{{ sessionLabel(w.session_type) }}</el-tag>
            </template>
            <div class="wk-row" v-if="w.purpose"><b>目的</b>{{ w.purpose }}</div>
            <div class="wk-row"><b>强度</b>{{ w.intensity_anchor?.label || sessionLabel(w.session_type) }}</div>
            <div class="wk-row" v-if="w.distance_km_range?.min"><b>距离</b>{{ w.distance_km_range.min }}-{{ w.distance_km_range.max }} km</div>
            <div class="wk-row" v-if="w.frequency_hint"><b>频率</b>{{ w.frequency_hint }}</div>
            <div class="wk-row" v-if="w.progression"><b>进阶</b>{{ w.progression }}</div>
            <div class="wk-row warn" v-if="w.cautions"><b>注意</b>{{ w.cautions }}</div>
          </el-collapse-item>
        </el-collapse>

        <h4>出处与证据（{{ detail.evidences?.length || 0 }}）</h4>
        <div class="d-note">{{ detail.evidence_note }}</div>
        <div class="src-row" v-for="(s, i) in detail.evidences" :key="i">
          <el-tag size="small" :type="evTagType(s.level)" effect="dark">{{ s.level }}</el-tag>
          <div>
            <a v-if="s.url" :href="s.url" target="_blank" rel="noopener">{{ s.title }}</a>
            <span v-else>{{ s.title }}</span>
            <div class="src-note" v-if="s.note">{{ s.note }}</div>
          </div>
        </div>

        <div class="apply-box">
          <div class="apply-t">想真实试一周？</div>
          <div class="apply-d">按你的真实数据（周跑量 / 可练时段 / 当前 VDOT）自动降档生成一周体验课表：
            质量课 ≤2 次、长距离 ≤ 周跑量 30%、配速全部按当前能力解析。生成后成为当前计划，可在「训练计划」页查看并同步手表。</div>
          <el-button type="primary" round :loading="applying" @click="applyWeek">按我的数据生成体验周</el-button>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../../api'

const router = useRouter()
const items = ref([])
const fOrigin = ref('')
const fLevel = ref(null)
const fQuery = ref('')
const drawer = ref(false)
const detail = ref(null)
const applying = ref(false)
const loading = ref(true)

const filtered = computed(() => items.value.filter(m =>
  (!fOrigin.value || (fOrigin.value === 'japan' ? ['japan', 'asia'].includes(m.origin) : m.origin === fOrigin.value))
  && (!fLevel.value || m.level === fLevel.value)
  && (!fQuery.value || [m.name_zh, m.name_ja, m.name_en, m.summary, ...(m.tags || [])].join(' ').toLowerCase().includes(fQuery.value.toLowerCase()))
))

const LEVEL = { beginner: '入门', intermediate: '进阶', advanced: '高级', elite: '精英' }
const ORIGIN = { japan: '日本', asia: '亚洲', western: '西方', global: '全球' }
const SESSION = { easy: '轻松跑', tempo: '节奏跑', interval: '间歇', long: '长距离', recovery: '恢复跑',
                  fartlek: '变速跑', hill: '坡地', race: '比赛', strength: '力量' }
const BASIS = { culture: '文化', time: '时间', climate: '气候', physique: '体形', note: '说明' }
const levelLabel = v => LEVEL[v] || v
const originLabel = v => ORIGIN[v] || v
const sessionLabel = v => SESSION[v] || v
const basisLabel = k => BASIS[k] || k
const evTagType = lv => ({ A: 'success', B: 'primary', C: 'warning', D: 'info' }[lv] || 'info')
const kmRange = m => {
  const r = m.weekly_km_range || {}
  return (r.min || r.max) ? `周跑量 ${r.min}-${r.max}km` : '跑量不限'
}

async function openDetail(code) {
  drawer.value = true
  detail.value = null
  try { detail.value = await api.get(`/methods/${code}`) } catch (e) { detail.value = { name_zh: '加载失败', summary: e.message } }
}

async function applyWeek() {
  const ok = await ElMessageBox.confirm(
    '将按你的真实数据生成一周体验课表，并设为当前训练计划（原有进行中计划会被归档）。继续？',
    '生成体验周', { type: 'warning', confirmButtonText: '生成', cancelButtonText: '取消' }).catch(() => false)
  if (!ok) return
  applying.value = true
  try {
    const r = await api.post(`/methods/${detail.value.code}/apply-week`)
    ElMessage.success(r.message || '体验周已生成')
    drawer.value = false
    router.push('/plan')
  } catch (e) {
    ElMessage.error(e.message || '生成失败')
  } finally { applying.value = false }
}

onMounted(async () => {
  loading.value = true
  try {
    const d = await api.get('/methods')
    items.value = d.items
  } finally { loading.value = false }
})
</script>

<style scoped>
.filter-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
.f-count { margin-left: auto; font-size: 12px; color: var(--text-3); }
.m-card { cursor: pointer; height: 100%; transition: border-color .15s; }
.m-card:hover { border-color: var(--lime-deep); }
.m-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
.m-name { font-size: 14px; line-height: 1.4; }
.m-sub { font-size: 11px; color: var(--text-3); margin-top: 3px; }
.m-fit { display: flex; align-items: center; gap: 8px; font-size: 11px; color: var(--text-3); margin-top: 10px; }
.m-fit b { color: var(--text); font-size: 12px; }
.m-dist { display: flex; height: 5px; border-radius: 3px; overflow: hidden; margin-top: 8px; background: var(--bg-inset); }
.md-seg { height: 100%; }
.m-meta { font-size: 11px; color: var(--text-3); margin-top: 7px; }
.m-tags { margin-top: 7px; display: flex; gap: 4px; flex-wrap: wrap; }
.detail { padding: 0 4px; }
.d-sub { color: var(--text-3); font-size: 12px; margin-bottom: 8px; }
.d-tags { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
.d-summary { font-size: 13px; line-height: 1.9; color: var(--text-2); }
.detail h4 { margin: 16px 0 8px; font-size: 13px; }
.d-list { padding-left: 18px; font-size: 12px; line-height: 1.9; color: var(--text-2); }
.d-pc-item { border-radius: 8px; padding: 8px 10px; font-size: 12px; line-height: 1.7; margin-bottom: 8px;
  color: var(--text-2); background: var(--bg-inset); border: 1px solid var(--border); }
.d-pc-item.pro .pc-t { color: var(--green); }
.d-pc-item.con .pc-t { color: var(--orange); }
.d-pc-item.warn { border-color: rgba(255, 162, 77, .35); }
.d-pc-item.warn .pc-t { color: var(--orange); }
.pc-t { font-size: 11px; margin-bottom: 3px; }
.basis-row { display: flex; gap: 10px; font-size: 12px; line-height: 1.7; margin-bottom: 5px; }
.b-k { flex-shrink: 0; width: 34px; color: var(--text-3); }
.b-v { color: var(--text-2); }
.wk-list :deep(.el-collapse-item__header) { font-size: 13px; }
.wk-title { font-weight: 600; }
.wk-row { font-size: 12px; line-height: 1.8; color: var(--text-2); margin-bottom: 5px; }
.wk-row b { display: inline-block; width: 40px; color: var(--text-3); font-weight: 400; }
.wk-row.warn { color: var(--orange); }
.d-note { font-size: 12px; line-height: 1.8; color: var(--text-3); margin-bottom: 10px; }
.src-row { display: flex; gap: 8px; font-size: 12px; margin-bottom: 10px; align-items: flex-start; }
.src-row a { color: var(--blue); text-decoration: none; line-height: 1.6; }
.src-note { color: var(--text-3); font-size: 11px; margin-top: 2px; }
.apply-box { margin-top: 18px; padding: 14px; border-radius: 10px;
  background: rgba(200, 241, 105, 0.06); border: 1px solid rgba(200, 241, 105, 0.25); }
.apply-t { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.apply-d { font-size: 12px; line-height: 1.7; color: var(--text-2); margin-bottom: 10px; }
</style>
