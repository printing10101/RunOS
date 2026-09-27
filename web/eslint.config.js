// ESLint 9 flat config（web/package.json 是 "type": "module"，本文件按 ESM 解析）。
//
// 规则集取向与仓库根 ruff.toml 一致：只开能抓到真问题的规则，不开纯风格门禁。
// 之所以用 flat/essential 而不是 flat/recommended：recommended 里大量是模板排版
// 与属性顺序偏好，7k 行既有代码全部套上去会一次性爆几百条噪音，反而没人看。
// 这里先锁「语法正确性 + 未定义名 + 未使用变量」，跑顺之后再按模块加。
import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'

export default [
  { ignores: ['dist/**', 'node_modules/**', 'vite.config.js.timestamp-*'] },
  js.configs.recommended,
  ...pluginVue.configs['flat/essential'],
  {
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: {
        ...globals.browser,
        ...globals.node,
        // <script setup> 的编译器宏由 vue 编译期注入，无需 import
        defineProps: 'readonly',
        defineEmits: 'readonly',
        defineOptions: 'readonly',
        defineExpose: 'readonly',
        defineModel: 'readonly',
        withDefaults: 'readonly',
      },
    },
    rules: {
      // 组件里常有意留空的 catch 或解构占位，命名前缀 _ 视为已使用
      'no-unused-vars': ['warn', { args: 'none', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' }],
      // 组件名多词规则属纯命名风格门禁：路由级视图按页面命名（Dashboard、Diet 等
      // 单词是既有约定），共享组件已用大驼峰多词名（RingGauge、RollNum），关闭之
      'vue/multi-word-component-names': 'off',
      // 本轮先不开（存量太大，开成 error 会挡住交付）：
      //   vue/attributes-order、vue/max-attributes-per-line 等排版类规则
    },
  },
]
