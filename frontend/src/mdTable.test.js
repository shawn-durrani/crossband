// Tables in chat messages on a phone (#567). A wide table broke its words
// into single letters to fit the screen, because table cells inherited the
// message's overflow-wrap: anywhere. Now a table keeps whole words and
// scrolls sideways inside its own box (.md-table, which Message.jsx puts
// round every table), so the page stays the width of the screen. These
// tests pin the rules in index.css that do it. The render smoke checks
// that every table in a message is inside the box.
// Run: node --test frontend/src/mdTable.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const css = readFileSync(new URL('./index.css', import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

// The declarations of every rule whose selector list names `selector`.
function declarations(selector) {
  const out = []
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (m[1].split(',').map((s) => s.trim()).includes(selector)) out.push(m[2])
  }
  return out.join(';')
}

test('a table in a message keeps whole words', () => {
  assert.match(declarations('.md-body table'), /overflow-wrap:\s*normal/)
  for (const cell of ['.md-body th', '.md-body td']) {
    assert.doesNotMatch(declarations(cell), /overflow-wrap|word-break|break-all|break-words/,
                        `${cell} must not break words again`)
  }
})

test('a wide table scrolls sideways inside its own box', () => {
  assert.match(declarations('.md-table'), /overflow-x:\s*auto/)
})

test('your own message is never wider than its column', () => {
  // Without this, a table with whole words pushes the right-aligned bubble
  // off the left edge of a phone instead of scrolling in its box.
  assert.match(declarations('.user-bubble'), /max-width:[^;]*100%/)
})
