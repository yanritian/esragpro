import assert from 'node:assert/strict'
import { afterEach, beforeEach, describe, test } from 'node:test'

import api, { AUTH_REQUIRED_EVENT, authApi } from '../src/api/index.js'
import { setSession } from '../src/auth/session.js'

function createStorage() {
  const values = new Map()
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null
    },
    setItem(key, value) {
      values.set(key, String(value))
    },
    removeItem(key) {
      values.delete(key)
    },
  }
}

describe('auth API integration', () => {
  beforeEach(() => {
    globalThis.localStorage = createStorage()
    globalThis.window = new EventTarget()
  })

  afterEach(() => {
    delete globalThis.localStorage
    delete globalThis.window
  })

  test('injects the current bearer token into API requests', async () => {
    setSession('access-token', { user_id: 'user-1', username: 'alice', role: 'user' })
    const requestInterceptor = api.interceptors.request.handlers[0].fulfilled
    const config = await requestInterceptor({ headers: {} })

    assert.equal(config.headers.Authorization, 'Bearer access-token')
  })

  test('does not overwrite an explicit bearer token on a request', async () => {
    setSession('stored-token', { user_id: 'user-1', username: 'alice', role: 'user' })
    const requestInterceptor = api.interceptors.request.handlers[0].fulfilled
    const config = await requestInterceptor({
      headers: { Authorization: 'Bearer one-time-token' },
    })

    assert.equal(config.headers.Authorization, 'Bearer one-time-token')
  })

  test('verifies the token with /auth/me before persisting the user session', async () => {
    const calls = []
    const originalPost = api.post
    const originalGet = api.get
    api.post = async (path, payload) => {
      calls.push({ method: 'post', path, payload })
      return { access_token: 'access-token', expires_in: 900 }
    }
    api.get = async (path, config) => {
      calls.push({ method: 'get', path, config })
      return { user_id: 'user-1', username: 'alice', email: null, role: 'user' }
    }

    try {
      const result = await authApi.login({ username: 'alice', password: 'secret' })

      assert.deepEqual(result.user, {
        user_id: 'user-1',
        username: 'alice',
        email: null,
        role: 'user',
      })
      assert.deepEqual(calls.map(({ method, path }) => ({ method, path })), [
        { method: 'post', path: '/auth/login' },
        { method: 'get', path: '/auth/me' },
      ])
      assert.equal(globalThis.localStorage.getItem('user').includes('secret'), false)
      assert.equal(globalThis.localStorage.getItem('token'), 'access-token')
    } finally {
      api.post = originalPost
      api.get = originalGet
    }
  })

  test('clears the session and emits one auth-required event for repeated 401 responses', async () => {
    setSession('expired-token', { user_id: 'user-1', username: 'alice', role: 'user' })
    const events = []
    window.addEventListener(AUTH_REQUIRED_EVENT, () => events.push(true))
    const responseInterceptor = api.interceptors.response.handlers[0].rejected
    const unauthorized = { response: { status: 401 } }

    await assert.rejects(responseInterceptor(unauthorized), (error) => error === unauthorized)
    await assert.rejects(responseInterceptor(unauthorized), (error) => error === unauthorized)

    assert.equal(globalThis.localStorage.getItem('token'), null)
    assert.equal(events.length, 1)
  })
})
