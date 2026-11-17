import { createRouter, createWebHistory } from 'vue-router'
import { isAuthenticated } from '../auth/session.js'

export const routes = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('../views/Login.vue'),
    meta: { public: true },
  },
  {
    path: '/',
    name: 'Home',
    component: () => import('../views/Home.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/search',
    name: 'Search',
    component: () => import('../views/Search.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/documents',
    name: 'Documents',
    component: () => import('../views/Documents.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/agentic',
    name: 'AgenticChat',
    component: () => import('../views/AgenticChat.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/evaluation',
    name: 'Evaluation',
    component: () => import('../views/Evaluation.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/graph',
    name: 'KnowledgeGraph',
    component: () => import('../views/KnowledgeGraph.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: { name: 'Home' },
  },
]

export function safeRedirect(value) {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) return '/'
  if (value === '/login' || value.startsWith('/login?')) return '/'
  return value
}

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  if (to.name === 'Login' && isAuthenticated()) {
    return safeRedirect(to.query.redirect)
  }

  if (to.meta.requiresAuth && !isAuthenticated()) {
    return {
      name: 'Login',
      query: { redirect: safeRedirect(to.fullPath) },
    }
  }

  return true
})

export default router
