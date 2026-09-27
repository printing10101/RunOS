<template>
  <TabPage title="训练知识库" sub="训练方法 · 训练原理 · 参考计划 —— 每一条都有出处与证据等级，AI 定制计划的依据都从这里来">
    <el-tabs v-model="tab">
      <el-tab-pane label="为我推荐" name="recommend" lazy><RecommendTab /></el-tab-pane>
      <el-tab-pane label="训练方法" name="methods" lazy><MethodsTab /></el-tab-pane>
      <el-tab-pane label="训练原理" name="principles" lazy><PrinciplesTab /></el-tab-pane>
      <el-tab-pane label="参考计划" name="plans" lazy><PlansTab /></el-tab-pane>
    </el-tabs>
  </TabPage>
</template>

<script setup>
import { ref } from 'vue'
import TabPage from '../components/TabPage.vue'
import RecommendTab from './library/RecommendTab.vue'
import MethodsTab from './library/MethodsTab.vue'
import PrinciplesTab from './library/PrinciplesTab.vue'
import PlansTab from './library/PlansTab.vue'

const tab = ref('recommend')
</script>
