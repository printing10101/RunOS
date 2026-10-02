<template>
  <XianxiaBackdrop />

  <!-- 移动端顶栏 + 抽屉导航：窄屏隐藏侧栏（打卡等日常动作多在手机上完成） -->
  <div class="mobile-topbar">
    <button class="mt-burger" aria-label="打开导航" @click="menuOpen = true">☰</button>
    <span class="mt-brand">RunOS</span>
  </div>
  <el-drawer v-model="menuOpen" direction="ltr" size="236px" :with-header="false">
    <div class="drawer-nav">
      <div class="brand" style="padding: 4px 6px 14px">
        <div class="brand-logo">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none">
            <ellipse cx="12" cy="14" rx="9" ry="5.5" stroke="#081210" stroke-width="2.4"/>
            <circle cx="17.5" cy="6" r="2.6" fill="#081210"/>
          </svg>
        </div>
        <div class="brand-name">RunOS</div>
      </div>
      <el-menu :default-active="$route.path" router @select="menuOpen = false">
        <el-menu-item v-for="n in NAV" :key="n.path" :index="n.path">
          <span class="mi">{{ n.icon }}</span>{{ n.label }}
        </el-menu-item>
      </el-menu>
    </div>
  </el-drawer>

  <el-container class="app-shell">
    <el-aside width="216px" class="app-aside">
      <div class="brand">
        <div class="brand-logo">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none">
            <ellipse cx="12" cy="14" rx="9" ry="5.5" stroke="#081210" stroke-width="2.4"/>
            <circle cx="17.5" cy="6" r="2.6" fill="#081210"/>
          </svg>
        </div>
        <div>
          <div class="brand-name">RunOS</div>
          <div class="brand-sub">TRAIN · ASSESS · PREDICT</div>
        </div>
      </div>

      <el-menu :default-active="$route.path" router class="app-menu">
        <el-menu-item v-for="n in NAV" :key="n.path" :index="n.path">
          <span class="mi">{{ n.icon }}</span>{{ n.label }}
        </el-menu-item>
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
          <div class="af-name" style="color:var(--jade)">完善个人档案</div>
          <div class="af-meta">录入真实数据，开启评估与计划</div>
        </div>
      </div>
    </el-aside>

    <el-main class="app-main">
      <router-view />
    </el-main>
  </el-container>

  <FirstRunDialog v-model="showOnboarding" />
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from './api'
import FirstRunDialog from './components/FirstRunDialog.vue'
import XianxiaBackdrop from './components/XianxiaBackdrop.vue'

const ONBOARDING_SKIP_KEY = 'runos:onboarding:skipped'
const route = useRoute()
const athlete = ref(null)
const runnerType = ref('')
const vdot = ref('-')
const showOnboarding = ref(false)
const menuOpen = ref(false)

// 侧栏与移动端抽屉共用一份导航清单（加页面时两处同步出现）
const NAV = [
  { path: '/', icon: '◆', label: '今日' },
  { path: '/plan', icon: '▤', label: '训练计划' },
  { path: '/activities', icon: '≡', label: '训练记录' },
  { path: '/status', icon: '◷', label: '训练状态' },
  { path: '/analysis', icon: '◈', label: '分析' },
  { path: '/stats', icon: '◫', label: '数据报表' },
  { path: '/diet', icon: '◍', label: '饮食' },
  { path: '/coach', icon: '✦', label: 'AI 教练' },
  { path: '/library', icon: '❖', label: '训练知识库' },
  { path: '/profile', icon: '☺', label: '个人档案' },
  { path: '/settings', icon: '✎', label: '设置' },
]
const age = computed(() =>
  athlete.value ? new Date().getFullYear() - athlete.value.birth_year : '-')

async function loadAthlete() {
  try {
    const d = await api.get('/athlete')
    athlete.value = d.athlete
    runnerType.value = d.runner_type && d.runner_type.event !== 'unknown' ? d.runner_type.type_name : ''
    // 档案切换（如清除演示数据）后旧 VDOT 不残留，允许回落到 '-'
    vdot.value = d.current_vdot || '-'
    // 首次使用且没跳过过引导：弹窗补齐高驰拿不到的身份信息（性别/出生年份/身高/体重）
    if (!d.athlete && !localStorage.getItem(ONBOARDING_SKIP_KEY)) showOnboarding.value = true
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
.app-shell { min-height: 100vh; position: relative; z-index: 1; background: transparent; }
.app-aside {
  /* 半透明 + 轻毛玻璃：山水场景从侧栏后透出 */
  background: linear-gradient(180deg, rgba(12, 24, 19, 0.86) 0%, rgba(8, 18, 14, 0.94) 100%);
  backdrop-filter: blur(6px);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
  position: sticky; top: 0; height: 100vh;
}
.brand { display: flex; gap: 11px; align-items: center; padding: 20px 18px 16px; border-bottom: 1px solid var(--border); }
.brand-logo {
  width: 38px; height: 38px; border-radius: 11px; flex-shrink: 0;
  background: linear-gradient(135deg, var(--jade) 0%, var(--jade-deep) 100%);
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 2px 14px rgba(63, 208, 164, 0.25);
}
.brand-name { color: var(--text); font-weight: 800; font-size: 14.5px; line-height: 1.25; letter-spacing: 0.02em; }
.brand-sub { color: var(--text-3); font-size: 9px; margin-top: 3px; letter-spacing: 0.22em; font-weight: 600; }

.app-menu { border-right: none; background: transparent; margin-top: 10px; flex: 1; padding: 0 10px; }
.app-menu .el-menu-item {
  color: var(--text-2); height: 44px; border-radius: 10px; margin-bottom: 3px;
  font-size: 14px; transition: all .15s;
}
.app-menu .el-menu-item:hover { background: rgba(157, 184, 173, 0.07); color: var(--text); }
.app-menu .el-menu-item.is-active {
  background: linear-gradient(90deg, rgba(63, 208, 164, 0.14), rgba(63, 208, 164, 0.04));
  color: var(--jade); font-weight: 700; box-shadow: inset 2.5px 0 0 var(--jade);
}
.mi { margin-right: 10px; font-size: 12px; color: inherit; opacity: .75; }

.aside-footer {
  display: flex; gap: 11px; align-items: center; margin: 12px; padding: 12px;
  border: 1px solid var(--border); border-radius: 12px; background: rgba(157, 184, 173, 0.05);
  cursor: pointer; transition: border-color .15s;
}
.aside-footer:hover { border-color: rgba(63, 208, 164, 0.4); }
.af-avatar { background: linear-gradient(135deg, #2a4a40, #14251f); color: var(--jade); font-weight: 800; }
.af-name { color: var(--text); font-size: 13px; font-weight: 700; display: flex; align-items: center; gap: 6px; }
.af-tag { border: none; background: rgba(63, 208, 164, 0.16); color: var(--jade); transform: scale(0.92); transform-origin: left center; font-weight: 700; }
.af-meta { color: var(--text-3); font-size: 11px; margin-top: 3px; }

.app-main { padding: 22px 28px; max-width: 1280px; margin: 0 auto; width: 100%; }

/* ---- 移动端（打卡等日常动作多在手机上完成）：隐藏侧栏，顶栏 + 抽屉导航 ---- */
.mobile-topbar { display: none; }
.mt-burger {
  background: none; border: 1px solid var(--border); color: var(--text);
  border-radius: 8px; font-size: 16px; line-height: 1; padding: 6px 10px; cursor: pointer;
}
.mt-brand { font-weight: 800; letter-spacing: 0.02em; }

@media (max-width: 820px) {
  .app-aside { display: none; }
  .mobile-topbar {
    display: flex; align-items: center; gap: 10px; padding: 10px 14px;
    position: sticky; top: 0; z-index: 30;
    background: rgba(8, 18, 16, 0.92); backdrop-filter: blur(6px);
    border-bottom: 1px solid var(--border);
  }
  .app-main { padding: 14px; }
}
.drawer-nav { padding: 14px 6px; }
</style>

<style>
/* 全局（非 scoped）：页面通用 */
body { margin: 0; background: var(--bg-page); color: var(--text);
  /* 拉丁字母与数字全走 Big Shoulders（2K 风格比分牌体），中文回退系统中文字体 */
  font-family: 'Big Shoulders Display', -apple-system, 'PingFang SC', 'Microsoft YaHei', 'Segoe UI', sans-serif; }
* { box-sizing: border-box; }
.card-grid { display: grid; gap: 14px; }
</style>
