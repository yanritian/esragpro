import assert from 'node:assert/strict'
import { describe, test } from 'node:test'
import { readFileSync } from 'node:fs'

const appSource = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')

function getCompactMediaBlock(source) {
  const match = source.match(/@media\s*\(max-width:\s*(\d+)px\)\s*\{([\s\S]*)\n\}/)
  assert.ok(match, 'App shell must define a compact responsive media query')
  return { maxWidth: Number(match[1]), css: match[2] }
}

describe('tablet shell responsive contract', () => {
  test('uses the compact shell at 768px without changing the desktop shell', () => {
    const { maxWidth, css } = getCompactMediaBlock(appSource)

    assert.ok(maxWidth >= 768, 'the compact breakpoint must include an exact 768px viewport')
    assert.ok(maxWidth < 1280, 'the compact breakpoint must leave the 1280px desktop shell unchanged')
    assert.match(css, /\.nav\s*\{[\s\S]*?display:\s*none;/)
    assert.match(css, /\.mobile-menu-button\s*\{[\s\S]*?display:\s*inline-flex;/)
  })
})
