// Resampling the mic to 16 kHz (#505).
// Run: node --test frontend/src/resample.test.js
//
// The old path kept every third sample and dropped the rest, so a sound
// above 8 kHz came out as a false one below it. What these pin: speech-band
// tones come through at their own level and timing, anything from 8 kHz is
// cut by at least 40 dB, a steady offset stays put, chunking changes
// nothing, and it works for 44.1 kHz, 48 kHz and rates that aren't round.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { Resampler, TARGET_RATE, designFilter, resample } from './resample.js'

const OUT = TARGET_RATE

function tone(rate, hz, seconds, amp = 0.5) {
  const x = new Float32Array(Math.round(rate * seconds))
  for (let i = 0; i < x.length; i++) x[i] = amp * Math.sin((2 * Math.PI * hz * i) / rate)
  return x
}

// Level in dB against a sine of amplitude `amp`, leaving out the edges.
function levelDb(y, amp) {
  let sum = 0
  const from = 200
  const to = y.length - 200
  for (let i = from; i < to; i++) sum += y[i] * y[i]
  return 20 * Math.log10(Math.sqrt(sum / (to - from)) / (amp / Math.SQRT2))
}

// Noise that is the same on every run.
function noise(n, seed = 1) {
  const x = new Float32Array(n)
  let s = seed
  for (let i = 0; i < n; i++) {
    s = (s * 1103515245 + 12345) % 2147483648
    x[i] = (s / 2147483648) * 2 - 1
  }
  return x
}

function concat(parts) {
  const all = new Float32Array(parts.reduce((n, p) => n + p.length, 0))
  let at = 0
  for (const p of parts) { all.set(p, at); at += p.length }
  return all
}

// Feed `x` in chunks of the given sizes, cycling through them, then flush.
function streamed(x, rate, sizes) {
  const r = new Resampler(rate)
  const parts = []
  let at = 0
  for (let i = 0; at < x.length; i++) {
    const size = sizes[i % sizes.length]
    parts.push(r.push(x.subarray(at, at + size)))
    at += size
  }
  parts.push(r.flush())
  return concat(parts)
}

for (const rate of [48000, 44100]) {
  test(`${rate} Hz: speech-band tones keep their level and timing`, () => {
    for (const hz of [150, 1000, 3000, 5500, 6900]) {
      const y = resample(tone(rate, hz, 0.5), rate)
      assert.ok(Math.abs(levelDb(y, 0.5)) < 0.1, `${hz} Hz level ${levelDb(y, 0.5)} dB`)
      // Each output sample is the tone at that sample's own time: nothing
      // shifted, nothing smeared. 0.005 is 40 dB under the tone.
      let worst = 0
      for (let k = 200; k < y.length - 200; k++) {
        const want = 0.5 * Math.sin((2 * Math.PI * hz * k) / OUT)
        worst = Math.max(worst, Math.abs(y[k] - want))
      }
      assert.ok(worst < 0.005, `${hz} Hz off by up to ${worst}`)
    }
  })

  test(`${rate} Hz: anything from 8 kHz up is cut by at least 40 dB`, () => {
    // Dropping samples would have turned each of these into a tone below
    // 8 kHz at full level.
    for (const hz of [8000, 8100, 8500, 9000, 10000, 12000, 15000, 20000, 21900]) {
      const y = resample(tone(rate, hz, 0.5), rate)
      assert.ok(levelDb(y, 0.5) <= -40, `${hz} Hz only down ${-levelDb(y, 0.5)} dB`)
    }
  })

  test(`${rate} Hz: a steady offset stays the same offset`, () => {
    const y = resample(new Float32Array(rate).fill(0.3), rate)
    assert.equal(y.length, OUT)
    for (let k = 0; k < y.length; k++) assert.ok(Math.abs(y[k] - 0.3) < 1e-6, `sample ${k} is ${y[k]}`)
    // With a tone on top, the average is still the offset.
    const mixed = tone(rate, 440, 1, 0.2).map((v) => v - 0.25)
    const out = resample(mixed, rate)
    const mean = out.reduce((a, b) => a + b, 0) / out.length
    assert.ok(Math.abs(mean + 0.25) < 1e-3, `mean ${mean}`)
  })

  test(`${rate} Hz: chunked output is exactly the one-shot output`, () => {
    const x = noise(rate)
    const whole = resample(x, rate)
    assert.equal(whole.length, Math.floor((x.length * OUT) / rate))
    // The mic's chunks, odd sizes, and one sample at a time for a while.
    for (const sizes of [[4096], [1, 7, 128, 1000, 3], [2048, 0, 333], [x.length]]) {
      assert.deepEqual(streamed(x, rate, sizes), whole, `chunks ${sizes}`)
    }
    const oneByOne = streamed(x.subarray(0, 5000), rate, [1])
    assert.deepEqual(oneByOne, resample(x.subarray(0, 5000), rate))
  })

  test(`${rate} Hz: a tone streamed in mic-sized chunks has no clicks at the edges`, () => {
    // The largest step between neighbouring samples of a 1 kHz tone at
    // 16 kHz is 2 sin(pi/16) of its amplitude. A click at a chunk edge
    // would be a bigger step.
    const y = streamed(tone(rate, 1000, 2), rate, [4096])
    const limit = 0.5 * 2 * Math.sin(Math.PI / 16) + 0.005
    for (let k = 1; k < y.length; k++) {
      assert.ok(Math.abs(y[k] - y[k - 1]) < limit, `step of ${Math.abs(y[k] - y[k - 1])} at ${k}`)
    }
  })

  test(`${rate} Hz: each mic chunk makes its share of 16 kHz, and the total keeps time`, () => {
    const r = new Resampler(rate)
    const chunk = noise(4096, 7)
    let total = 0
    const sizes = new Set()
    for (let i = 0; i < 400; i++) {
      const n = r.push(chunk).length
      if (i > 0) sizes.add(n)
      total += n
    }
    const each = (4096 * OUT) / rate
    for (const n of sizes) assert.ok(Math.abs(n - each) < 1, `a chunk made ${n}, expected about ${each}`)
    // What's still owed is only the filter's look-ahead, under 2 ms.
    const owed = (400 * 4096 * OUT) / rate - total
    assert.ok(owed >= 0 && owed < 0.002 * OUT, `owed ${owed}`)
  })
}

test('a rate that is not a round number works the same way', () => {
  for (const rate of [47999, 44056]) {
    const y = resample(tone(rate, 2000, 0.5), rate)
    assert.equal(y.length, Math.floor((Math.round(rate * 0.5) * OUT) / rate))
    assert.ok(Math.abs(levelDb(y, 0.5)) < 0.1)
    assert.ok(levelDb(resample(tone(rate, 11000, 0.5), rate), 0.5) <= -40)
    const dc = resample(new Float32Array(rate).fill(-0.4), rate)
    assert.ok(dc.every((v) => Math.abs(v + 0.4) < 1e-4))
    const x = noise(rate, 3)
    assert.deepEqual(streamed(x, rate, [4096]), resample(x, rate))
  }
})

test('other common rates: 16 kHz passes through, lower rates are interpolated', () => {
  const x = noise(1600, 5)
  assert.deepEqual(resample(x, 16000), x)
  assert.deepEqual(streamed(x, 16000, [100, 3]), x)
  for (const rate of [8000, 22050, 24000, 32000, 96000]) {
    const y = resample(tone(rate, 1000, 0.5), rate)
    assert.equal(y.length, Math.floor((Math.round(rate * 0.5) * OUT) / rate))
    assert.ok(Math.abs(levelDb(y, 0.5)) < 0.1, `${rate} Hz level ${levelDb(y, 0.5)}`)
  }
})

test('reset starts a fresh stream, with no trace of what came before', () => {
  const r = new Resampler(48000)
  r.push(new Float32Array(4096).fill(0.9))
  r.reset()
  const after = r.push(tone(48000, 500, 0.1))
  const fresh = new Resampler(48000).push(tone(48000, 500, 0.1))
  assert.deepEqual(after, fresh)
})

test('the start of a stream does not ramp up from silence', () => {
  const y = new Resampler(44100).push(new Float32Array(4096).fill(0.5))
  assert.ok(y.length > 0)
  assert.ok(Math.abs(y[0] - 0.5) < 1e-6)
})

test('nothing in, nothing out', () => {
  assert.equal(resample(new Float32Array(0), 48000).length, 0)
  // two samples at 48 kHz are less than one sample at 16 kHz
  assert.equal(resample(new Float32Array(2), 48000).length, 0)
  const r = new Resampler(48000)
  assert.equal(r.push(new Float32Array(0)).length, 0)
  assert.equal(r.flush().length, 0)
  assert.throws(() => new Resampler(0), RangeError)
})

test('the cost stays small enough for a phone', () => {
  // Every output sample costs one multiply per tap, so the tap count is
  // the cost: about 2.8 million multiplies a second at 48 kHz.
  assert.ok(designFilter(48000).taps <= 176)
  assert.ok(designFilter(44100).taps <= 160)
  // A long stream doesn't grow its buffer past a chunk and one filter.
  const r = new Resampler(44100)
  const chunk = noise(4096, 9)
  for (let i = 0; i < 1000; i++) r.push(chunk)
  assert.ok(r.buf.length <= 2 * (4096 + designFilter(44100).taps), `buffer ${r.buf.length}`)
})
