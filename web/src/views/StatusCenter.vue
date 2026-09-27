<template>
  <TabPage title="训练状态" sub="当前身体正处在什么状态：负荷 / 恢复 / 准备度，官方数据与引擎估算互为对照">
    <el-tabs v-model="tab">
      <el-tab-pane label="训练状态" name="status" lazy>
        <TrainingStatus />
      </el-tab-pane>
      <el-tab-pane label="身体与恢复" name="body" lazy>
        <Health />
      </el-tab-pane>
    </el-tabs>
  </TabPage>
</template>

<script setup>
import { ref } from 'vue'
import TabPage from '../components/TabPage.vue'
import TrainingStatus from './TrainingStatus.vue'
import Health from './Health.vue'

const tab = ref('status')
</script>
