<template>
  <NumberFlow v-if="hasValue" :value="value" :locales="locales" class="roll-num" />
  <span v-else class="roll-fallback">{{ fallback }}</span>
</template>

<script setup>
// 滚动数字：数值变化时平滑滚动，依赖 @number-flow/vue。
// value 为 null/undefined 时显示 fallback（'-'），不渲染 0——「没有数据」和
// 「数据是 0」是两回事。
import { computed } from 'vue'
import NumberFlow from '@number-flow/vue'

const props = defineProps({
  value: { type: Number, default: null },
  fallback: { type: String, default: '-' },
  digits: { type: Number, default: 0 },
})

const hasValue = computed(() => props.value != null && !isNaN(props.value))
const locales = computed(() => ({
  maximumFractionDigits: props.digits,
  minimumFractionDigits: 0,
}))
</script>

<style scoped>
.roll-num { font: inherit; color: inherit; }
.roll-fallback { color: inherit; }
</style>
