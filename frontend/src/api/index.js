import axios from 'axios'
import {
  clearSession,
  getToken,
  onSessionChange,
  setSession,
} from '../auth/session.js'

export const AUTH_REQUIRED_EVENT = 'esragpro:auth-required'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
})

let authRequiredNotified = false

function emitAuthRequired() {
  if (authRequiredNotified) return
  authRequiredNotified = true

  if (typeof globalThis.window?.dispatchEvent !== 'function') return
  const event = typeof globalThis.CustomEvent === 'function'
    ? new CustomEvent(AUTH_REQUIRED_EVENT)
    : new Event(AUTH_REQUIRED_EVENT)
  globalThis.window.dispatchEvent(event)
}

onSessionChange((session) => {
  if (session.token) authRequiredNotified = false
})

api.interceptors.request.use(
  (config) => {
    const token = getToken()
    if (token && !config.headers?.Authorization) {
      config.headers = config.headers || {}
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => {
    return Promise.reject(error)
  }
)

api.interceptors.response.use(
  (response) => response.data,
  (error) => {
    if (error.response?.status === 401) {
      clearSession()
      emitAuthRequired()
    }
    console.error('API Error:', error.response?.status || error.message)
    return Promise.reject(error)
  }
)

export const authApi = {
  async login(credentials) {
    const tokenResponse = await api.post('/auth/login', credentials)
    const accessToken = tokenResponse?.access_token
    if (!accessToken) throw new Error('Login response did not include an access token')

    const user = await api.get('/auth/me', {
      headers: { Authorization: `Bearer ${accessToken}` },
    })
    const session = setSession(accessToken, user)
    return { ...tokenResponse, user: session.user }
  },

  getCurrentUser() {
    return api.get('/auth/me')
  },
}

export default api
