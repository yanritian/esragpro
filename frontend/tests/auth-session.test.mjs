import assert from 'node:assert/strict'
import { afterEach, beforeEach, describe, test } from 'node:test'

import {
  clearSession,
  getSession,
  getToken,
  getUser,
  isAuthenticated,
  onSessionChange,
  setSession,
} from '../src/auth/session.js'

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

describe('auth session', () => {
  beforeEach(() => {
    globalThis.localStorage = createStorage()
  })

  afterEach(() => {
    delete globalThis.localStorage
  })

  test('stores a bearer token and sanitized user metadata without a password', () => {
    const user = {
      user_id: 'user-1',
      username: 'alice',
      email: 'alice@example.com',
      role: 'admin',
      created_at: '2026-09-20T00:00:00Z',
      password: 'must-not-persist',
    }

    setSession('access-token', user)

    assert.equal(getToken(), 'access-token')
    assert.equal(isAuthenticated(), true)
    assert.deepEqual(getUser(), {
      user_id: 'user-1',
      username: 'alice',
      email: 'alice@example.com',
      role: 'admin',
      created_at: '2026-09-20T00:00:00Z',
    })
    assert.equal(globalThis.localStorage.getItem('password'), null)
    assert.equal(globalThis.localStorage.getItem('user').includes('must-not-persist'), false)
  })

  test('notifies subscribers when the session is created and cleared', () => {
    const changes = []
    const unsubscribe = onSessionChange((session) => changes.push(session))

    setSession('access-token', { user_id: 'user-1', username: 'alice', role: 'user' })
    clearSession()
    unsubscribe()

    assert.equal(changes.length, 2)
    assert.equal(changes[0].token, 'access-token')
    assert.equal(changes[1].token, null)
    assert.equal(getSession().user, null)
  })

  test('does not expose stale user metadata when the bearer token is absent', () => {
    globalThis.localStorage.setItem('user', JSON.stringify({
      user_id: 'user-1',
      username: 'alice',
      role: 'user',
    }))

    assert.equal(isAuthenticated(), false)
    assert.equal(getUser(), null)
  })
})
