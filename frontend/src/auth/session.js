const SAFE_USER_FIELDS = ['user_id', 'username', 'email', 'role', 'created_at']

export const STORAGE_KEYS = Object.freeze({
  token: 'token',
  user: 'user',
})

export const AUTH_CHANGED_EVENT = 'esragpro:auth-changed'

const subscribers = new Set()

function getStorage() {
  return typeof globalThis.localStorage === 'undefined' ? null : globalThis.localStorage
}

function readToken() {
  const token = getStorage()?.getItem(STORAGE_KEYS.token)
  return typeof token === 'string' && token.trim() ? token : null
}

function readUser() {
  const serialized = getStorage()?.getItem(STORAGE_KEYS.user)
  if (!serialized) return null

  try {
    const parsed = JSON.parse(serialized)
    return sanitizeUser(parsed)
  } catch {
    return null
  }
}

export function sanitizeUser(user) {
  if (!user || typeof user !== 'object' || Array.isArray(user)) return null

  const sanitized = {}
  for (const field of SAFE_USER_FIELDS) {
    if (Object.prototype.hasOwnProperty.call(user, field)) {
      sanitized[field] = user[field]
    }
  }
  return Object.keys(sanitized).length ? sanitized : null
}

export function getSession() {
  const token = readToken()
  return {
    token,
    user: token ? readUser() : null,
  }
}

export function getToken() {
  return getSession().token
}

export function getUser() {
  return getSession().user
}

export function isAuthenticated() {
  return Boolean(getToken())
}

function notify() {
  const session = getSession()
  for (const subscriber of subscribers) {
    subscriber(session)
  }

  if (typeof globalThis.window?.dispatchEvent === 'function') {
    const event = typeof globalThis.CustomEvent === 'function'
      ? new CustomEvent(AUTH_CHANGED_EVENT, { detail: session })
      : new Event(AUTH_CHANGED_EVENT)
    globalThis.window.dispatchEvent(event)
  }
}

export function setSession(accessToken, user) {
  const response = accessToken && typeof accessToken === 'object' ? accessToken : null
  const tokenValue = typeof accessToken === 'string' ? accessToken : response?.access_token
  const token = typeof tokenValue === 'string' ? tokenValue.trim() : ''
  if (!token) {
    clearSession()
    return getSession()
  }

  const storage = getStorage()
  storage?.setItem(STORAGE_KEYS.token, token)
  const sanitizedUser = sanitizeUser(user || response?.user)
  if (sanitizedUser) {
    storage?.setItem(STORAGE_KEYS.user, JSON.stringify(sanitizedUser))
  } else {
    storage?.removeItem(STORAGE_KEYS.user)
  }
  notify()
  return getSession()
}

export function clearSession() {
  const hadSession = Boolean(readToken() || readUser())
  const storage = getStorage()
  storage?.removeItem(STORAGE_KEYS.token)
  storage?.removeItem(STORAGE_KEYS.user)
  if (hadSession) notify()
}

export function onSessionChange(subscriber) {
  subscribers.add(subscriber)
  return () => subscribers.delete(subscriber)
}
