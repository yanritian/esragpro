<template>
  <el-config-provider :locale="zhCn">
    <div class="app-container">
      <el-container>
        <el-header class="app-header">
          <div class="logo">
            <el-icon :size="28"><Document /></el-icon>
            <span>esRAGPro</span>
          </div>
          <div v-if="!isLoginRoute" class="nav">
            <el-menu mode="horizontal" :default-active="activeMenu" router>
              <el-menu-item index="/">首页</el-menu-item>
              <el-menu-item index="/documents">文档管理</el-menu-item>
              <el-menu-item index="/search">知识检索</el-menu-item>
            </el-menu>
          </div>
          <el-button
            v-if="!isLoginRoute"
            class="mobile-menu-button"
            type="primary"
            text
            circle
            aria-controls="mobile-navigation"
            :aria-expanded="mobileMenuOpen"
            :aria-label="mobileMenuOpen ? '关闭导航' : '打开导航'"
            @click="mobileMenuOpen = true"
          >
            <el-icon><Menu /></el-icon>
          </el-button>
          <div class="user-info">
            <el-dropdown v-if="currentUser" trigger="click" @command="handleUserCommand">
              <el-button class="user-trigger" link type="primary" :aria-label="`用户 ${currentUser.username}`">
                <el-icon><UserFilled /></el-icon>
                <span class="user-name">{{ currentUser.username }}</span>
                <el-icon><ArrowDown /></el-icon>
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="logout">退出登录</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-button v-else type="primary" size="small" :disabled="isLoginRoute" @click="goLogin">登录</el-button>
          </div>
        </el-header>
        <el-main class="app-main">
          <router-view />
        </el-main>
      </el-container>
    </div>
    <el-drawer v-if="!isLoginRoute" v-model="mobileMenuOpen" title="导航" direction="rtl" size="280px">
      <el-menu
        id="mobile-navigation"
        class="mobile-nav"
        :default-active="activeMenu"
        router
        @select="closeMobileMenu"
      >
        <el-menu-item index="/">首页</el-menu-item>
        <el-menu-item index="/documents">文档管理</el-menu-item>
        <el-menu-item index="/search">知识检索</el-menu-item>
      </el-menu>
    </el-drawer>
  </el-config-provider>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import zhCn from 'element-plus/dist/locale/zh-cn.mjs'
import { AUTH_REQUIRED_EVENT } from './api'
import { clearSession, getSession, onSessionChange } from './auth/session.js'
import { safeRedirect } from './router'

const route = useRoute()
const router = useRouter()
const activeMenu = computed(() => route.path)
const isLoginRoute = computed(() => route.name === 'Login')
const currentUser = ref(getSession().user)
const mobileMenuOpen = ref(false)

const closeMobileMenu = () => {
  mobileMenuOpen.value = false
}

const goLogin = () => {
  if (isLoginRoute.value) return
  router.push({
    name: 'Login',
    query: { redirect: safeRedirect(route.fullPath) },
  })
}

const logout = () => {
  clearSession()
  router.replace({ name: 'Login' })
}

const handleUserCommand = (command) => {
  if (command === 'logout') logout()
}

const handleAuthRequired = () => {
  currentUser.value = getSession().user
  if (!isLoginRoute.value) {
    router.replace({
      name: 'Login',
      query: { redirect: safeRedirect(route.fullPath) },
    })
  }
}

let stopSessionSubscription
onMounted(() => {
  stopSessionSubscription = onSessionChange((session) => {
    currentUser.value = session.user
  })
  window.addEventListener(AUTH_REQUIRED_EVENT, handleAuthRequired)
})

onUnmounted(() => {
  stopSessionSubscription?.()
  window.removeEventListener(AUTH_REQUIRED_EVENT, handleAuthRequired)
})
</script>

<style>
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  background-color: #f5f7fa;
  width: 100%;
  max-width: 100%;
  min-width: 0;
  overflow-x: hidden;
}

html {
  width: 100%;
  max-width: 100%;
  overflow-x: hidden;
}

#app,
.app-container {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  min-height: 100vh;
}

.app-container > .el-container,
.app-main,
.app-main > * {
  min-width: 0;
  max-width: 100%;
}

.app-header {
  background: #fff;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  height: 60px;
}

.logo {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 20px;
  font-weight: 600;
  color: #409eff;
}

.nav .el-menu {
  border-bottom: none;
}

.mobile-menu-button {
  display: none;
}

.mobile-nav {
  border-right: none;
}

.user-info {
  display: flex;
  align-items: center;
  flex-shrink: 0;
}

.user-trigger {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.user-name {
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.user-trigger:focus-visible,
.mobile-menu-button:focus-visible {
  outline: 2px solid #409eff;
  outline-offset: 2px;
}

.app-main {
  padding: 24px;
  min-height: calc(100vh - 60px);
  overflow-x: hidden;
}

@media (max-width: 1023px) {
  .app-header {
    height: 56px;
    padding: 0 16px;
  }

  .logo {
    font-size: 18px;
  }

  .user-name {
    max-width: 96px;
  }

  .nav {
    display: none;
  }

  .mobile-menu-button {
    display: inline-flex;
    margin-left: auto;
    margin-right: 8px;
  }

  .app-main {
    padding: 16px;
    min-height: calc(100vh - 56px);
  }

  .el-col-6,
  .el-col-8,
  .el-col-12,
  .el-col-18 {
    flex: 0 0 100%;
    max-width: 100%;
  }

  .el-row,
  .el-card {
    min-width: 0;
    max-width: 100%;
  }
}
</style>
