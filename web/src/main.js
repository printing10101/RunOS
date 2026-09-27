import { createApp } from 'vue'
import ElementPlus, { ElMessage } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import '@fontsource/barlow-condensed/500.css'
import '@fontsource/barlow-condensed/600.css'
import '@fontsource/barlow-condensed/700.css'
import './styles.css'
import App from './App.vue'
import router from './router'

document.documentElement.classList.add('dark')

const app = createApp(App)
app.use(ElementPlus, { locale: zhCn })
app.use(router)

// 全局兜底：组件内没 catch 的异步错误在这里弹出提示（api.js 的 normalizeApiError
// 注释承诺了这个兜底）。只提示不吞，控制台仍能看到完整堆栈。
app.config.errorHandler = (err) => {
  console.error(err)
  ElMessage.error(err && err.message ? err.message : '发生未知错误')
}

app.mount('#app')
