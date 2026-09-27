<template>
  <TabPage title="分析" sub="能力结构与短板：综合评估、成绩预测，每条结论都能回溯到引擎证据">
    <el-tabs v-model="tab">
      <el-tab-pane label="综合评估" name="assessment" lazy>
        <Assessment />
      </el-tab-pane>
      <el-tab-pane label="成绩预测" name="prediction" lazy>
        <Prediction />
      </el-tab-pane>
    </el-tabs>
  </TabPage>
</template>

<script setup>
import { ref } from 'vue'
import TabPage from '../components/TabPage.vue'
import Assessment from './Assessment.vue'
import Prediction from './Prediction.vue'

const tab = ref('assessment')
</script>
