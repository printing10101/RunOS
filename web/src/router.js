import { createRouter, createWebHistory } from 'vue-router'

// 按路由懒加载：首屏只加载当前页面代码，ECharts 等重库各自分包按需引入
const Dashboard = () => import('./views/Dashboard.vue')
const PlanCenter = () => import('./views/PlanCenter.vue')
const RecordsCenter = () => import('./views/RecordsCenter.vue')
const StatusCenter = () => import('./views/StatusCenter.vue')
const AnalysisCenter = () => import('./views/AnalysisCenter.vue')
const SettingsCenter = () => import('./views/SettingsCenter.vue')
const StatsReport = () => import('./views/StatsReport.vue')
const Diet = () => import('./views/Diet.vue')
const AiCoach = () => import('./views/AiCoach.vue')
const MethodLibrary = () => import('./views/MethodLibrary.vue')
const ActivityDetail = () => import('./views/ActivityDetail.vue')

// 页面组织（16 个入口合并为 9 个，语义重复的页面归入同一 Tab 容器）：
//   训练计划 = 计划课表 + 可训练时段   训练记录 = 训练明细 + 装备跑鞋
//   训练状态 = 负荷与状态 + 身体数据   分析 = 评估 + 预测 + 比赛 + 力量
//   设置 = 档案 + 平台连接
// 旧路径全部重定向，站内既有跳转无需改动。
const routes = [
  { path: '/', name: 'dashboard', component: Dashboard, meta: { title: '总览' } },
  { path: '/plan', name: 'plan', component: PlanCenter, meta: { title: '训练计划' } },
  { path: '/activities', name: 'activities', component: RecordsCenter, meta: { title: '训练记录' } },
  { path: '/activities/:id', name: 'activity-detail', component: ActivityDetail, meta: { title: '活动详情' } },
  { path: '/status', name: 'status', component: StatusCenter, meta: { title: '训练状态' } },
  { path: '/analysis', name: 'analysis', component: AnalysisCenter, meta: { title: '分析' } },
  { path: '/stats', name: 'stats', component: StatsReport, meta: { title: '数据报表' } },
  { path: '/coach', name: 'coach', component: AiCoach, meta: { title: 'AI 教练' } },
  { path: '/library', name: 'library', component: MethodLibrary, meta: { title: '训练知识库' } },
  { path: '/diet', name: 'diet', component: Diet, meta: { title: '饮食' } },
  { path: '/settings', name: 'settings', component: SettingsCenter, meta: { title: '设置' } },

  // 旧路径重定向（含 tab 深链）
  { path: '/schedule', redirect: '/plan?tab=schedule' },
  { path: '/gear', redirect: '/activities?tab=gear' },
  { path: '/health', redirect: '/status?tab=body' },
  { path: '/assessment', redirect: '/analysis?tab=assessment' },
  { path: '/prediction', redirect: '/analysis?tab=prediction' },
  { path: '/races', redirect: '/analysis?tab=races' },
  { path: '/strength', redirect: '/analysis?tab=strength' },
  { path: '/profile', redirect: '/settings?tab=profile' },
  { path: '/connections', redirect: '/settings?tab=connections' },
]

export default createRouter({ history: createWebHistory(), routes })
