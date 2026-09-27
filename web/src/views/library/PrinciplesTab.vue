<template>
  <div>
    <el-alert type="info" :closable="false" style="margin-bottom:14px"
      title="AI 定制计划的每个决策都应引用这里的原理与规则 —— 「为什么这么练」比「练什么」更值得先搞清楚。" />

    <div class="filter-bar">
      <el-radio-group v-model="fCat" size="small">
        <el-radio-button v-for="c in cats" :key="c.value" :value="c.value">{{ c.label }}</el-radio-button>
      </el-radio-group>
      <span class="f-count">{{ filtered.length }} 条原理</span>
    </div>

    <el-row :gutter="14">
      <el-col :span="12" v-for="p in filtered" :key="p.code" style="margin-bottom:14px">
        <el-card shadow="never" class="p-card">
          <div class="p-head">
            <b class="p-name">{{ p.name_zh }}</b>
            <el-tag size="small" :type="evTagType(p.evidence_level)" effect="dark">证据 {{ p.evidence_level }}</el-tag>
          </div>
          <div class="p-en" v-if="p.name_en">{{ p.name_en }}</div>
          <p class="p-summary">{{ p.summary }}</p>

          <el-collapse class="p-more">
            <el-collapse-item title="机制与规则">
              <div class="pb-title">生理机制</div>
              <div class="pb-body">{{ p.mechanism }}</div>
              <div class="pb-title" style="margin-top:10px">可执行规则</div>
              <ul class="pb-list"><li v-for="(r, i) in p.practical_rules" :key="i">{{ r }}</li></ul>
              <div class="pb-title" style="margin-top:10px">本平台已落地</div>
              <div class="pb-body platform">{{ p.platform_usage }}</div>
              <div class="pb-title" style="margin-top:10px">出处与证据边界</div>
              <div class="pb-body" style="color: var(--text-3)">{{ p.evidence_note }}</div>
              <div class="pb-src" v-for="(s, i) in p.sources" :key="i">
                <el-tag size="small" :type="evTagType(s.level)" effect="dark">{{ s.level }}</el-tag>
                <div>
                  <a v-if="s.url" :href="s.url" target="_blank" rel="noopener">{{ s.title }}</a>
                  <span v-else>{{ s.title }}</span>
                  <div class="src-note" v-if="s.note">{{ s.note }}</div>
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
const fCat = ref('')

const CATS = [
  { value: '', label: '全部' }, { value: 'load', label: '负荷' }, { value: 'intensity', label: '强度' },
  { value: 'periodization', label: '周期化' }, { value: 'recovery', label: '恢复' },
  { value: 'specificity', label: '专项化' }, { value: 'strength', label: '力量' },
  { value: 'nutrition', label: '营养' }, { value: 'heat', label: '环境' },
  { value: 'technique', label: '技术' }, { value: 'psychology', label: '心理' },
]
const cats = CATS
const evTagType = lv => ({ A: 'success', B: 'primary', C: 'warning', D: 'info' }[lv] || 'info')

const filtered = computed(() => items.value.filter(p => !fCat.value || p.category === fCat.value))

onMounted(async () => {
  const d = await api.get('/methods/principles')
  items.value = d.items
})
</script>

<style scoped>
.filter-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
.f-count { margin-left: auto; font-size: 12px; color: var(--text-3); }
.p-card { height: 100%; }
.p-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
.p-name { font-size: 14px; line-height: 1.4; }
.p-en { font-size: 11px; color: var(--text-3); margin-top: 3px; }
.p-summary { font-size: 12px; line-height: 1.8; color: var(--text-2); margin: 10px 0 4px; }
.p-more { border-top: 1px dashed var(--border); }
.pb-title { font-size: 11px; color: var(--text-3); margin-bottom: 4px; }
.pb-body { font-size: 12px; line-height: 1.8; color: var(--text-2); }
.pb-body.platform { color: var(--jade-deep); }
.pb-list { margin: 0; padding-left: 18px; font-size: 12px; line-height: 1.8; color: var(--text-2); }
.pb-src { display: flex; gap: 8px; font-size: 12px; margin-top: 8px; align-items: flex-start; }
.pb-src a { color: var(--blue); text-decoration: none; line-height: 1.6; }
.src-note { color: var(--text-3); font-size: 11px; margin-top: 2px; }
</style>
