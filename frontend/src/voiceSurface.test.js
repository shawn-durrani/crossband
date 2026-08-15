// Voice survives page switches (#69). A live session swaps its call surface
// for the compact strip while a full page is open, and never ends because a
// panel opened.
import assert from 'node:assert/strict'
import test from 'node:test'

import { voiceSurface } from './voiceSurface.js'

test('no session, no surface - whatever page is open', () => {
  assert.equal(voiceSurface({ voiceState: 'off', pageOpen: false }), 'none')
  assert.equal(voiceSurface({ voiceState: 'off', pageOpen: true }), 'none')
  assert.equal(voiceSurface({ voiceState: undefined, pageOpen: true }), 'none')
})

test('a live session keeps its call surface until a page opens (#69)', () => {
  for (const s of ['listening', 'transcribing', 'speaking']) {
    assert.equal(voiceSurface({ voiceState: s, pageOpen: false }), 'call')
    // the page does not end voice - it swaps the surface for the strip
    assert.equal(voiceSurface({ voiceState: s, pageOpen: true }), 'strip')
  }
})
