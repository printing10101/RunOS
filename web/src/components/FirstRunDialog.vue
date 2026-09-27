<template>
  <el-dialog :model-value="modelValue" width="470px" title="欢迎使用 RunOS"
             :close-on-click-modal="false" :close-on-press-escape="false" :show-close="false"
             class="onboard-dialog">
    <div class="onboard-intro">
      先花 30 秒补齐几项身份信息——<strong>性别、出生年份、身高体重无法从高驰同步</strong>，
      但训练配速区间、负荷计算与综合评估都依赖它们。
      静息心率、最大心率、HRV 基准无需手填，会随同步数据自动校准。
    </div>

    <el-form ref="formRef" :model="form" :rules="rules" label-width="88px">
      <el-form-item label="姓名" prop="name">
        <el-input v-model="form.name" placeholder="用于界面称呼，可留空" maxlength="20" />
      </el-form-item>
      <el-form-item label="性别" prop="sex">
        <el-radio-group v-model="form.sex">
          <el-radio value="male">男</el-radio>
          <el-radio value="female">女</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="出生年份" prop="birth_year">
        <el-input-number v-model="form.birth_year" :min="1930" :max="2015" style="width:100%" />
      </el-form-item>
      <el-form-item label="身高 (cm)" prop="height_cm">
        <el-input-number v-model="form.height_cm" :min="120" :max="230" style="width:100%" />
      </el-form-item>
      <el-form-item label="体重 (kg)" prop="weight_kg">
        <el-input-number v-model="form.weight_kg" :min="30" :max="200" style="width:100%" />
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="skip">跳过，稍后在设置里填写</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存并开始</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

defineProps({ modelValue: Boolean })
const emit = defineEmits(['update:modelValue'])

const formRef = ref()
const saving = ref(false)
const form = reactive({ name: '', sex: null, birth_year: null, height_cm: null, weight_kg: null })
const rules = {
  sex: [{ required: true, message: '请选择性别', trigger: 'change' }],
  birth_year: [{ required: true, message: '请填写出生年份', trigger: 'blur' }],
  height_cm: [{ required: true, message: '请填写身高', trigger: 'blur' }],
  weight_kg: [{ required: true, message: '请填写体重', trigger: 'blur' }],
}

function skip() {
  // 记住跳过：之后不再主动弹（用户随时可在设置 → 档案补填）
  localStorage.setItem('runos:onboarding:skipped', '1')
  emit('update:modelValue', false)
}

async function save() {
  await formRef.value.validate()
  saving.value = true
  try {
    const body = {}
    // 只提交有值字段：与档案页同一语义（留空 = 交给系统自动解析）
    Object.keys(form).forEach(k => { if (form[k] !== null && form[k] !== '') body[k] = form[k] })
    await api.put('/athlete', body)
    window.dispatchEvent(new CustomEvent('athlete-updated'))
    ElMessage.success('档案已创建，开始使用吧')
    emit('update:modelValue', false)
  } catch (e) {
    ElMessage.error(e.message || '保存失败')
  } finally { saving.value = false }
}
</script>

<style scoped>
.onboard-intro {
  background: var(--bg-inset);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 14px;
  font-size: 13px;
  color: var(--text-2);
  line-height: 1.7;
  margin-bottom: 16px;
}
</style>
