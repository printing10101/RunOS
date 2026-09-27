<template>
  <el-container class="app-shell">
    <el-aside width="216px" class="app-aside">
      <div class="brand">
        <div class="brand-logo">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none">
            <ellipse cx="12" cy="14" rx="9" ry="5.5" stroke="#0b0f14" stroke-width="2.4"/>
            <circle cx="17.5" cy="6" r="2.6" fill="#0b0f14"/>
          </svg>
        </div>
        <div>
          <div class="brand-name">RunOS</div>
          <div class="brand-sub">TRAIN · ASSESS · PREDICT</div>
        </div>
      </div>

      <el-menu :default-active="$route.path" router class="app-menu">
        <el-menu-item index="/"><span class="mi">◆</span>今日</el-menu-item>
        <el-menu-item index="/plan"><span class="mi">▤</span>训练计划</el-menu-item>
        <el-menu-item index="/activities"><span class="mi">≡</span>训练记录</el-menu-item>
        <el-menu-item index="/status"><span class="mi">◷</span>训练状态</el-menu-item>
        <el-menu-item index="/analysis"><span class="mi">◈</span>分析</el-menu-item>
        <el-menu-item index="/stats"><span class="mi">◫</span>数据报表</el-menu-item>
        <el-menu-item index="/diet"><span class="mi">◍</span>饮食</el-menu-item>
        <el-menu-item index="/coach"><span class="mi">✦</span>AI 教练</el-menu-item>
        <el-menu-item index="/library"><span class="mi">❖</span>训练知识库</el-menu-item>
        <el-menu-item index="/settings"><span class="mi">✎</span>设置</el-menu-item>
      </el-menu>

      <div class="aside-footer" v-if="athlete" @click="$router.push('/profile')">
        <el-avatar size="36" class="af-avatar">{{ athlete.name?.slice(0, 1) }}</el-avatar>
        <div class="af-body">
          <div class="af-name">{{ athlete.name }}
            <el-tag v-if="runnerType" size="small" effect="dark" class="af-tag">{{ runnerType }}</el-tag>
          </div>
          <div class="af-meta">{{ age }} 岁 · {{ athlete.sex === 'male' ? '男' : '女' }} · VDOT {{ vdot }}</div>
        </div>
      </div>
      <div class="aside-footer" v-else @click="$router.push('/profile')">
        <el-avatar size="36" class="af-avatar">+</el-avatar>
        <div class="af-body">
          <div class="af-name" style="color:var(--lime)">完善个人档案</div>
          <div class="af-meta">录入真实数据，开启评估与计划</div>
        </div>
      </div>
    </el-aside>

    <el-main class="app-main">
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from './api'

const route = useRoute()
const athlete = ref(null)
const runnerType = ref('')
const vdot = ref('-')
const age = computed(() =>
  athlete.value ? new Date().getFullYear() - athlete.value.birth_year : '-')

async function loadAthlete() {
  try {
    const d = await api.get('/athlete')
    athlete.value = d.athlete
    runnerType.value = d.runner_type && d.runner_type.event !== 'unknown' ? d.runner_type.type_name : ''
    // 档案切换（如清除演示数据）后旧 VDOT 不残留，允许回落到 '-'
    vdot.value = d.current_vdot || '-'
  } catch { /* 未初始化时忽略 */ }
}

onMounted(() => {
  loadAthlete()
  // 档案保存后立即刷新左下角（由 Profile.vue 派发事件）
  window.addEventListener('athlete-updated', loadAthlete)
})

// 左下角档案面板是常驻组件，只在启动时加载一次；
// 从 /profile 切出时重新拉取，保证保存后的数据能显示。
watch(() => route.path, (p, prev) => {
  if (prev === '/profile' && p !== '/profile') loadAthlete()
})
</script>

<style scoped>
.app-shell { min-height: 100vh; background: var(--bg-page); }
.app-aside {
  background: linear-gradient(180deg, #0d1319 0%, #0b0f14 100%);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
  position: sticky; top: 0; height: 100vh;
}
.brand { display: flex; gap: 11px; align-items: center; padding: 20px 18px 16px; border-bottom: 1px solid var(--border); }
.brand-logo {
  width: 38px; height: 38px; border-radius: 11px; flex-shrink: 0;
  background: linear-gradient(135deg, var(--lime) 0%, var(--lime-deep) 100%);
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 2px 14px rgba(200, 241, 105, 0.25);
}
.brand-name { color: var(--text); font-weight: 800; font-size: 14.5px; line-height: 1.25; letter-spacing: 0.02em; }
.brand-sub { color: var(--text-3); font-size: 9px; margin-top: 3px; letter-spacing: 0.22em; font-weight: 600; }

.app-menu { border-right: none; background: transparent; margin-top: 10px; flex: 1; padding: 0 10px; }
.app-menu .el-menu-item {
  color: var(--text-2); height: 44px; border-radius: 10px; margin-bottom: 3px;
  font-size: 14px; transition: all .15s;
}
.app-menu .el-menu-item:hover { background: rgba(148, 163, 184, 0.07); color: var(--text); }
.app-menu .el-menu-item.is-active {
  background: linear-gradient(90deg, rgba(200, 241, 105, 0.14), rgba(200, 241, 105, 0.04));
  color: var(--lime); font-weight: 700; box-shadow: inset 2.5px 0 0 var(--lime);
}
.mi { margin-right: 10px; font-size: 12px; color: inherit; opacity: .75; }

.aside-footer {
  display: flex; gap: 11px; align-items: center; margin: 12px; padding: 12px;
  border: 1px solid var(--border); border-radius: 12px; background: rgba(148, 163, 184, 0.05);
  cursor: pointer; transition: border-color .15s;
}
.aside-footer:hover { border-color: rgba(200, 241, 105, 0.4); }
.af-avatar { background: linear-gradient(135deg, #2b3646, #1a2330); color: var(--lime); font-weight: 800; }
.af-name { color: var(--text); font-size: 13px; font-weight: 700; display: flex; align-items: center; gap: 6px; }
.af-tag { border: none; background: rgba(200, 241, 105, 0.16); color: var(--lime); transform: scale(0.92); transform-origin: left center; font-weight: 700; }
.af-meta { color: var(--text-3); font-size: 11px; margin-top: 3px; }

.app-main { padding: 22px 28px; max-width: 1280px; margin: 0 auto; width: 100%; }
</style>

<style>
/* 全局（非 scoped）：页面通用 */
body { margin: 0; background: var(--bg-page); color: var(--text);
  /* 拉丁字母与数字全走 Big Shoulders（2K 风格比分牌体），中文回退系统中文字体 */
  font-family: 'Big Shoulders Display', -apple-system, 'PingFang SC', 'Microsoft YaHei', 'Segoe UI', sans-serif; }
* { box-sizing: border-box; }
.card-grid { display: grid; gap: 14px; }
</style>
