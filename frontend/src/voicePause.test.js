import { test } from 'node:test'
import assert from 'node:assert/strict'
import { DEFAULT_PAUSE_SECS, loadPauseSecs, savePauseSecs } from './voicePause.js'

function storage(initial = {}) {
  const m = new Map(Object.entries(initial))
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    map: m,
  }
}

test('a fresh browser starts on the 1.5 second default', () => {
  assert.equal(DEFAULT_PAUSE_SECS, 1.5)
  assert.equal(loadPauseSecs(storage()), 1.5)
})

test('the old auto-saved 2.0 is dropped once, so the new default applies', () => {
  const s = storage({ voice_silence_secs: '2' })
  assert.equal(loadPauseSecs(s), 1.5)
  assert.equal(s.map.has('voice_silence_secs'), false)
})

test('a pause picked on the slider is kept, 2.0 included', () => {
  const s = storage()
  savePauseSecs(s, 2)
  assert.equal(loadPauseSecs(s), 2)
  savePauseSecs(s, 1.2)
  assert.equal(loadPauseSecs(s), 1.2)
})

test('a value other than 2.0 saved before the mark existed was picked by hand', () => {
  assert.equal(loadPauseSecs(storage({ voice_silence_secs: '2.6' })), 2.6)
  assert.equal(loadPauseSecs(storage({ voice_silence_secs: '0.9' })), 0.9)
})

test('junk or a blocked storage falls back to the default', () => {
  assert.equal(loadPauseSecs(storage({ voice_silence_secs: 'abc' })), 1.5)
  assert.equal(loadPauseSecs(storage({ voice_silence_secs: '0' })), 1.5)
  const blocked = { getItem() { throw new Error('denied') }, setItem() { throw new Error('denied') } }
  assert.equal(loadPauseSecs(blocked), 1.5)
  assert.doesNotThrow(() => savePauseSecs(blocked, 1.5))
})
