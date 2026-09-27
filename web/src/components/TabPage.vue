<template>
  <div class="tab-page">
    <div class="tp-head">
      <h2 class="tp-title">{{ title }}</h2>
      <span v-if="sub" class="tp-sub">{{ sub }}</span>
    </div>
    <div class="tp-body">
      <slot />
    </div>
  </div>
</template>

<script setup>
// 中心页骨架：标题 + 副标题 + 内容容器。各「中心」视图与知识库共用，
// 保证页面头部节奏一致。
defineProps({
  title: { type: String, required: true },
  sub: { type: String, default: '' },
})
</script>

<style scoped>
.tp-head { display: flex; align-items: baseline; gap: 12px; margin: 4px 0 14px; }
.tp-title { font-size: 20px; font-weight: 800; margin: 0; color: var(--text); }
.tp-sub { font-size: 12px; color: var(--text-3); }
.tp-body :deep(.el-card) { border-color: var(--border); }
</style>
