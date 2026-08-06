// Tests for the voice call screen's state rules.
// Run: node --test frontend/src/voiceView.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { orbStateFor, statusFor, showInterruptHint, displayPartial } from './voiceView.js'

test('muting stops the orb claiming it can hear you', () => {
  // The bug this pins: muted sessions kept the "listening" breathing —
  // the screen said one thing while the mic did another.
  assert.equal(orbStateFor('listening', true), 'idle')
  assert.equal(orbStateFor('transcribing', true), 'idle')
  assert.equal(orbStateFor('listening', false), 'listening')
  assert.equal(orbStateFor('transcribing', false), 'transcribing')
})

test('a speaking model keeps the speaking visual even while muted', () => {
  // Who's talking still matters with your mic off.
  assert.equal(orbStateFor('speaking', true), 'speaking')
  assert.equal(orbStateFor('speaking', false), 'speaking')
})

test('status priority: held > muted > speaking > thinking > listening', () => {
  assert.match(statusFor({ held: 2, muted: true, voiceState: 'speaking' }), /Holding 2 messages/)
  assert.match(statusFor({ held: 1, voiceState: 'listening' }), /Holding 1 message —/)
  assert.match(statusFor({ muted: true, voiceState: 'listening' }), /Muted — models keep talking/)
  assert.equal(statusFor({ muted: true, voiceState: 'speaking', speakerName: 'Claude' }), 'Claude…')
  assert.equal(statusFor({ voiceState: 'speaking' }), 'Speaking…')
  assert.equal(statusFor({ voiceState: 'transcribing' }), 'Thinking…')
  assert.equal(statusFor({ voiceState: 'listening' }), 'Listening…')
})

test('the interrupt hint never lies', () => {
  // Only while a model speaks AND a voice barge-in could actually work.
  assert.equal(showInterruptHint('speaking', false), true)
  assert.equal(showInterruptHint('speaking', true), false)  // mic off → can't interrupt
  assert.equal(showInterruptHint('listening', false), false)
  assert.equal(showInterruptHint('transcribing', false), false)
})

test('partials show only when there is something to show', () => {
  // The realtime stream emits plenty of empty/whitespace partials.
  assert.equal(displayPartial('  '), null)
  assert.equal(displayPartial(''), null)
  assert.equal(displayPartial(null), null)
  assert.equal(displayPartial(' okay and what about '), 'okay and what about')
})
