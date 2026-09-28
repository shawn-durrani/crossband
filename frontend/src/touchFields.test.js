// Text fields at 16px on touch screens (#558). iOS Safari zooms the page
// in when you tap a field under 16px and doesn't zoom back out, so the zoom
// stuck as the owner moved around the app on his phone. index.css lifts
// every field to 16px under (pointer: coarse). These tests pin that rule,
// and the things that could quietly beat it.
// Run: node --test frontend/src/touchFields.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'

const css = readFileSync(new URL('./index.css', import.meta.url), 'utf8')
const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8')

// The body of the top-level @media (pointer: coarse) block, and the brace
// depth it opens at (0 = unlayered, which is what beats Tailwind's utilities).
function coarseBlock(source) {
  const at = source.indexOf('@media (pointer: coarse)')
  if (at < 0) return null
  let depth = 0
  for (const ch of source.slice(0, at).replace(/\/\*[\s\S]*?\*\//g, '')) {
    if (ch === '{') depth += 1
    if (ch === '}') depth -= 1
  }
  const open = source.indexOf('{', at)
  let d = 0
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === '{') d += 1
    if (source[i] === '}') d -= 1
    if (d === 0) return { depth, body: source.slice(open + 1, i) }
  }
  return null
}

test('a coarse-pointer rule sets every kind of text field to 16px', () => {
  const block = coarseBlock(css)
  assert.ok(block, 'index.css has no @media (pointer: coarse) block')
  const rule = block.body.match(/([^{}]+)\{([^{}]*font-size:\s*16px[^{}]*)\}/)
  assert.ok(rule, 'the coarse-pointer block sets no font-size: 16px')
  const selectors = rule[1].split(',').map((s) => s.trim())
  assert.ok(selectors.includes('textarea'), 'textarea is missing')
  assert.ok(selectors.includes('select'), 'select is missing')
  assert.ok(selectors.some((s) => s.startsWith('[contenteditable]')), 'contenteditable is missing')
  const input = selectors.find((s) => s.startsWith('input'))
  assert.ok(input, 'input is missing')
  // Text-like types must stay covered: only non-text controls are left out.
  for (const type of ['text', 'password', 'search', 'email', 'url', 'number', 'tel']) {
    assert.ok(!input.includes(`[type="${type}"]`), `input[type=${type}] is excluded`)
  }
})

test('the rule sits outside any layer, so text-xs and text-sm cannot beat it', () => {
  assert.equal(coarseBlock(css).depth, 0)
})

test('the viewport tag still lets you pinch to zoom', () => {
  const meta = html.match(/<meta[^>]+name="viewport"[^>]*>/)
  assert.ok(meta, 'index.html has no viewport tag')
  assert.doesNotMatch(meta[0], /maximum-scale|user-scalable\s*=\s*(no|0)/)
})

test('no field sets an inline or !important font size that would win on a phone', () => {
  const dir = new URL('./components/', import.meta.url)
  const files = readdirSync(dir).filter((f) => f.endsWith('.jsx'))
    .map((f) => new URL(f, dir))
  files.push(new URL('./App.jsx', import.meta.url), new URL('./AuthGate.jsx', import.meta.url))
  for (const file of files) {
    const src = readFileSync(file, 'utf8')
    for (const m of src.matchAll(/<(input|textarea|select)\b([\s\S]*?)(?<!=)>/g)) {
      assert.doesNotMatch(m[2], /fontSize|!text-|text-[^\s'"`]*!/,
        `${file.pathname.split('/').pop()}: a <${m[1]}> overrides its font size`)
    }
  }
})
