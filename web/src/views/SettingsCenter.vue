<template>
  <TabPage title="设置" sub="档案是所有评估的输入：身份事实由你提供，可解析字段随数据实时更新">
    <el-tabs v-model="tab">
      <el-tab-pane label="个人档案" name="profile" lazy>
        <Profile />
      </el-tab-pane>
      <el-tab-pane label="平台连接" name="connections" lazy>
        <Connections />
      </el-tab-pane>
    </el-tabs>
  </TabPage>
</template>

<script setup>
import { ref } from 'vue'
import TabPage from '../components/TabPage.vue'
import Profile from './Profile.vue'
import Connections from './Connections.vue'

const tab = ref('profile')
</script>
