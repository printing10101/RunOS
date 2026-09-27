<template>
  <div class="ring-wrap">
    <svg class="ring" :width="size" :height="size" viewBox="0 0 120 120">
      <circle class="ring-bg" cx="60" cy="60" :r="radius" />
      <circle class="ring-fg" cx="60" cy="60" :r="radius"
              :stroke="color" :stroke-dasharray="dash" :stroke-dashoffset="offset" />
    </svg>
    <div class="ring-center">
      <div class="num-display ring-label">
        {{ formattedLabel }}<span v-if="unit" class="ring-unit">{{ unit }}</span>
      </div>
      <div class="ring-sub">{{ sub }}</div>
      <div v-if="sub2" class="ring-sub2">{{ sub2 }}</div>
    </div>
  </div>
</template>

<script setup>
// 环形进度：value 是 0-100 的百分比；label 是环心主数字（一般放绝对值而不是百分比，
// 如「42.5km」，此时用 digits 控制小数位），sub/sub2 是环下方的说明行。
import { computed } from 'vue'

const props = defineProps({
  value: { type: Number, default: 0 },
  label: { type: [Number, String], default: '-' },
  digits: { type: Number, default: 0 },
  unit: { type: String, default: '' },
  sub: { type: String, default: '' },
  sub2: { type: String, default: '' },
  color: { type: String, default: '#c8f169' },
  size: { type: Number, default: 132 },
})

const radius = 52
const CIRC = 2 * Math.PI * radius
const dash = `${CIRC} ${CIRC}`

const offset = computed(() => {
  const pct = Math.max(0, Math.min(100, props.value || 0))
  return CIRC * (1 - pct / 100)
})

const formattedLabel = computed(() => {
  const v = Number(props.label)
  if (props.label === '-' || props.label == null || isNaN(v)) return String(props.label)
  return v.toFixed(props.digits)
})
</script>

<style scoped>
.ring-wrap { position: relative; display: inline-flex; }
.ring { transform: rotate(-90deg); }
.ring-bg, .ring-fg { fill: none; stroke-width: 9; stroke-linecap: round; }
.ring-bg { stroke: rgba(148, 163, 184, 0.12); }
.ring-fg { transition: stroke-dashoffset 0.8s cubic-bezier(0.22, 1, 0.36, 1); }
.ring-center {
  position: absolute; inset: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 2px; text-align: center;
}
.ring-label { font-size: 26px; font-weight: 700; color: var(--text); }
.ring-unit { font-size: 12px; color: var(--text-3); margin-left: 2px; }
.ring-sub { font-size: 12px; color: var(--text-3); }
.ring-sub2 { font-size: 11px; color: var(--text-2); }
</style>
