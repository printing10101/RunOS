<template>
  <TabPage title="训练计划" sub="课表管理 · 参考计划库 —— 计划变更走「AI 提案 → 确认」，高驰课表在平台连接页镜像">
    <el-tabs v-model="tab">
      <el-tab-pane label="我的课表" name="plan" lazy>
        <PlanView />
      </el-tab-pane>
      <el-tab-pane label="参考计划库" name="library" lazy>
        <PlansTab />
      </el-tab-pane>
    </el-tabs>
  </TabPage>
</template>

<script setup>
import { ref } from 'vue'
import TabPage from '../components/TabPage.vue'
import PlanView from './PlanView.vue'
import PlansTab from './library/PlansTab.vue'

const tab = ref('plan')
</script>
