// The identity copy of a batch-transcribed turn (#461): the cut that keeps
// only this turn out of the batch recording, and the WAV the server reads.

import test from 'node:test'
import assert from 'node:assert/strict'
import { IDENTITY_RATE, MAX_CLIP_MS, PRE_ROLL_MS, TAIL_MS, batchSttForm, clipWindow,
         identityWav } from './identityClip.js'

test('the window is the speech, the pre-roll and a short tail', () => {
  // the pause that ended the turn ran 2s past the last voiced frame
  assert.deepEqual(clipWindow({ speechMs: 2400, endedAt: 10_000, stoppedAt: 12_000 }),
                   { keepMs: 2400 + PRE_ROLL_MS + TAIL_MS, dropMs: 2000 - TAIL_MS })
  // push-to-talk stops on the tap, so there is nothing after the speech
  assert.deepEqual(clipWindow({ speechMs: 2400, endedAt: 10_000, stoppedAt: 10_000 }),
                   { keepMs: 2400 + PRE_ROLL_MS, dropMs: 0 })
})

test('a salvage long after the turn drops what came after it', () => {
  // the salvage timer fired 9s after the speech ended: whatever the mic
  // heard since, the models or someone else, stays out of the copy
  assert.deepEqual(clipWindow({ speechMs: 1500, endedAt: 10_000, stoppedAt: 19_000 }),
                   { keepMs: 1500 + PRE_ROLL_MS + TAIL_MS, dropMs: 9000 - TAIL_MS })
})

test('a turn that cannot be placed sends no copy', () => {
  assert.equal(clipWindow(), null)
  assert.equal(clipWindow({ speechMs: 0, endedAt: 10_000, stoppedAt: 11_000 }), null)
  // a rescued turn has no end-of-speech time to cut from
  assert.equal(clipWindow({ speechMs: 2000, endedAt: undefined, stoppedAt: 11_000 }), null)
  assert.equal(clipWindow({ speechMs: 2000, endedAt: 0, stoppedAt: 11_000 }), null)
  // a clock that ran backwards is not a turn
  assert.equal(clipWindow({ speechMs: 2000, endedAt: 12_000, stoppedAt: 11_000 }), null)
})

test('the window never passes what the server keeps', () => {
  assert.equal(clipWindow({ speechMs: 500_000, endedAt: 1, stoppedAt: 2 }).keepMs, MAX_CLIP_MS)
})

function readWav(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const text = (at, n) => String.fromCharCode(...bytes.slice(at, at + n))
  return {
    riff: text(0, 4), wave: text(8, 4), fmt: text(12, 4), data: text(36, 4),
    format: view.getUint16(20, true), channels: view.getUint16(22, true),
    rate: view.getUint32(24, true), bits: view.getUint16(34, true),
    dataBytes: view.getUint32(40, true), riffBytes: view.getUint32(4, true),
    sample: (i) => view.getInt16(44 + i * 2, true),
  }
}

test('the copy is a 16 kHz mono PCM-16 WAV of the turn only', () => {
  // three seconds at 48 kHz: quiet before, the turn loud in the middle
  // second, and someone else after it
  const src = new Float32Array(144_000)
  src.fill(0.5, 48_000, 96_000)
  src.fill(-0.75, 96_000)
  const wav = readWav(identityWav(src, 48_000, { keepMs: 1000, dropMs: 1000 }))
  assert.equal(wav.riff, 'RIFF')
  assert.equal(wav.wave, 'WAVE')
  assert.equal(wav.fmt, 'fmt ')
  assert.equal(wav.data, 'data')
  assert.equal(wav.format, 1)
  assert.equal(wav.channels, 1)
  assert.equal(wav.rate, IDENTITY_RATE)
  assert.equal(wav.bits, 16)
  assert.equal(wav.dataBytes, IDENTITY_RATE * 2)   // one second, no more
  assert.equal(wav.riffBytes, 36 + wav.dataBytes)
  // only the turn's second made it, from its first sample to its last
  assert.equal(wav.sample(0), Math.trunc(0.5 * 0x7fff))
  assert.equal(wav.sample(IDENTITY_RATE - 1), Math.trunc(0.5 * 0x7fff))
})

test('a window longer than the recording keeps what there is', () => {
  const src = new Float32Array(16_000).fill(-0.25)
  const wav = readWav(identityWav(src, 16_000, { keepMs: 60_000, dropMs: 0 }))
  assert.equal(wav.dataBytes, 32_000)
  assert.equal(wav.sample(0), -0.25 * 0x8000)
})

test('a recording that began after the turn ended has nothing to send', () => {
  const src = new Float32Array(16_000).fill(0.5)
  assert.equal(identityWav(src, 16_000, { keepMs: 2000, dropMs: 1000 }), null)
  assert.equal(identityWav(src, 16_000, { keepMs: 2000, dropMs: 5000 }), null)
})

test('samples past full scale are clipped, never wrapped', () => {
  const wav = readWav(identityWav(Float32Array.from([2, -2]), 16_000,
                                  { keepMs: 1000, dropMs: 0 }))
  assert.equal(wav.sample(0), 0x7fff)
  assert.equal(wav.sample(1), -0x8000)
})

test('nothing to send is null', () => {
  const win = { keepMs: 1000, dropMs: 0 }
  assert.equal(identityWav(new Float32Array(0), 48_000, win), null)
  assert.equal(identityWav(null, 48_000, win), null)
  assert.equal(identityWav(new Float32Array(10), 48_000, null), null)
  assert.equal(identityWav(new Float32Array(10), 0, win), null)
  // fewer source samples than one output sample
  assert.equal(identityWav(new Float32Array(2), 48_000, win), null)
})

test('the upload carries the copy and the turn id together, or neither', () => {
  const rec = new Blob(['webm'], { type: 'audio/webm' })
  const copy = new Blob([identityWav(new Float32Array(1600).fill(0.1), 16_000,
                                    { keepMs: 1000, dropMs: 0 })],
                        { type: 'audio/wav' })
  const full = batchSttForm(rec, 1499.6, 't1', copy)
  assert.deepEqual([...full.keys()], ['file', 'duration_ms', 'turn_id', 'pcm'])
  assert.equal(full.get('duration_ms'), '1500')
  assert.equal(full.get('turn_id'), 't1')
  assert.equal(full.get('pcm').size, copy.size)
  assert.equal(full.get('pcm').name, 'turn.wav')
  assert.equal(full.get('file').name, 'utterance.webm')
  // no copy, or no turn id: the upload is what it always was
  assert.deepEqual([...batchSttForm(rec, 900, 't1', null).keys()], ['file', 'duration_ms'])
  assert.deepEqual([...batchSttForm(rec, 900, null, copy).keys()], ['file', 'duration_ms'])
})
