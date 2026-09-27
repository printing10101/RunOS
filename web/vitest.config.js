import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// 单独一个 vitest.config.js 而不是塞进 vite.config.js 的 test 字段：
// 构建配置只服务 npm run dev/build，测试另有 jsdom 环境与用例目录，
// 分开之后两边互不牵连，改构建不会误改测试行为。
export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.spec.js'],
    // 单测不碰真实后端；api.js 里的 axios 由用例自己 vi.mock
    restoreMocks: true,
  },
})
