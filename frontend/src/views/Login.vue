<template>
  <main class="login-page">
    <section class="login-panel" aria-labelledby="login-title">
      <div class="login-brand">
        <el-icon :size="28" color="#409eff"><Document /></el-icon>
        <span>esRAGPro</span>
      </div>
      <h1 id="login-title">登录知识库</h1>
      <p class="login-subtitle">使用企业账号访问可信知识与检索服务</p>

      <el-alert
        v-if="errorMessage"
        class="login-alert"
        :title="errorMessage"
        type="error"
        show-icon
        :closable="false"
      />

      <el-form
        ref="formRef"
        class="login-form"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="submit"
      >
        <el-form-item label="用户名" prop="username">
          <el-input
            v-model="form.username"
            size="large"
            autocomplete="username"
            placeholder="请输入用户名"
            :prefix-icon="User"
          />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            size="large"
            type="password"
            autocomplete="current-password"
            placeholder="请输入密码"
            show-password
            :prefix-icon="Lock"
          />
        </el-form-item>
        <el-button
          class="login-submit"
          type="primary"
          size="large"
          native-type="submit"
          :loading="loading"
        >
          登录
        </el-button>
      </el-form>
    </section>
  </main>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { User, Lock } from '@element-plus/icons-vue'
import { authApi } from '../api'
import { safeRedirect } from '../router'

const route = useRoute()
const router = useRouter()
const formRef = ref()
const loading = ref(false)
const errorMessage = ref('')
const form = reactive({
  username: '',
  password: '',
})

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

function getErrorMessage(error) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail) return detail
  if (error instanceof Error && error.message) return error.message
  return '登录失败，请检查用户名和密码后重试'
}

async function submit() {
  errorMessage.value = ''
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    await authApi.login({
      username: form.username.trim(),
      password: form.password,
    })
    await router.replace(safeRedirect(route.query.redirect))
  } catch (error) {
    errorMessage.value = getErrorMessage(error)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  --ui-bg: #f5f7fa;
  --ui-surface: #ffffff;
  --ui-text-primary: #303133;
  --ui-text-secondary: #606266;
  --ui-border: #ebeef5;
  --ui-primary: #409eff;
  --ui-danger: #f56c6c;
  min-height: calc(100vh - 60px);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px 16px;
  background: var(--ui-bg);
}

.login-panel {
  width: min(100%, 420px);
  padding: 32px;
  border: 1px solid var(--ui-border);
  border-radius: 4px;
  background: var(--ui-surface);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}

.login-brand {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 20px;
  color: var(--ui-primary);
  font-size: 20px;
  font-weight: 600;
}

h1 {
  margin: 0;
  color: var(--ui-text-primary);
  font-size: 24px;
  line-height: 1.3;
}

.login-subtitle {
  margin: 8px 0 24px;
  color: var(--ui-text-secondary);
  font-size: 14px;
  line-height: 1.5;
}

.login-alert {
  margin-bottom: 16px;
}

.login-form {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.login-submit {
  width: 100%;
  margin-top: 8px;
}

@media (max-width: 767px) {
  .login-page {
    min-height: calc(100vh - 56px);
    align-items: flex-start;
    padding: 24px 16px;
  }

  .login-panel {
    padding: 24px 20px;
  }
}
</style>
