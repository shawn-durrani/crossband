// Resampling the microphone to 16 kHz (#505), in a pure module
// per the house rule so the filter is unit-tested.
//
// The browser records at its own rate, usually 44.1 or 48 kHz, and the
// transcriber and the voice checks read 16 kHz. Keeping every third sample
// and throwing the rest away folds everything above 8 kHz back into the
// speech band as noise that was never there: hiss, a tap, a TV's sibilants.
// So every output sample is a weighted sum of the input around it, and the
// weights are a low-pass filter that keeps speech up to 7 kHz flat and cuts
// everything from 8 kHz by 60 dB before anything is dropped.
//
// The filter is a windowed sinc (Kaiser window), cut into one row of taps
// per phase: where an output sample falls between two input samples. 48 kHz
// needs one row, 44.1 kHz needs 160, and a rate that isn't a round number
// uses the nearest of 1024 rows. Each row sums to exactly one, so silence
// stays silence and a steady offset comes out unchanged.
//
// Streaming: the mic arrives in chunks, and the filter reaches across chunk
// edges, so the resampler keeps the input it still needs between calls.
// Feeding a recording in pieces gives the same samples as feeding it whole.
// An output sample leaves once the input half a filter past it has arrived,
// under 2 ms of audio at 44.1 or 48 kHz, and its timing matches the
// input's exactly.

export const TARGET_RATE = 16000

// The edges, as fractions of the lower of the two rates. At 16 kHz out:
// flat to 7 kHz, cut from 8 kHz, which is as high as 16 kHz can carry.
const PASS_EDGE = 7 / 16
const STOP_EDGE = 8 / 16
const STOP_DB = 60
// Phase rows kept at most. A rate whose exact ratio needs more rounds each
// output to the nearest row, a timing error under a thousandth of a sample.
const MAX_PHASES = 1024

function gcd(a, b) {
  while (b) [a, b] = [b, a % b]
  return a
}

// The modified Bessel function the Kaiser window is built from.
function besselI0(x) {
  const q = (x * x) / 4
  let sum = 1
  let term = 1
  for (let k = 1; k < 64; k++) {
    term *= q / (k * k)
    sum += term
    if (term < sum * 1e-15) break
  }
  return sum
}

// The filter for one pair of rates: `taps` weights per row, `phases + 1`
// rows, row p for an output sample p/phases of the way to the next input
// sample. The extra last row is the first one a sample later, so rounding
// up to it needs no special case.
export function designFilter(srcRate, dstRate = TARGET_RATE) {
  const g = gcd(srcRate, dstRate)
  const up = dstRate / g
  const down = srcRate / g
  const phases = Math.min(up, MAX_PHASES)
  const low = Math.min(srcRate, dstRate)
  // In cycles per input sample.
  const cutoff = ((PASS_EDGE + STOP_EDGE) / 2) * (low / srcRate)
  const width = (STOP_EDGE - PASS_EDGE) * (low / srcRate)
  // Kaiser's estimates for the length and the window's shape. An even
  // length puts the filter's centre on a whole sample.
  let taps = Math.ceil((STOP_DB - 7.95) / (14.36 * width))
  taps += taps % 2
  const beta = 0.1102 * (STOP_DB - 8.7)
  const half = taps / 2
  const norm = besselI0(beta)
  const rows = new Float64Array((phases + 1) * taps)
  for (let p = 0; p <= phases; p++) {
    let sum = 0
    for (let j = 0; j < taps; j++) {
      // How far input sample j sits from the output sample, in samples.
      const t = j + p / phases - half
      const x = 2 * cutoff * t
      const sinc = x === 0 ? 1 : Math.sin(Math.PI * x) / (Math.PI * x)
      const edge = t / half
      const w = edge >= 1 || edge <= -1 ? 0 : besselI0(beta * Math.sqrt(1 - edge * edge)) / norm
      rows[p * taps + j] = sinc * w
      sum += sinc * w
    }
    for (let j = 0; j < taps; j++) rows[p * taps + j] /= sum
  }
  return { srcRate, dstRate, up, down, phases, taps, rows }
}

// A filter is only ever read, so every stream at the same rates shares one,
// and rebuilding a dead mic processor doesn't design it again.
const designed = new Map()
function filterFor(srcRate, dstRate) {
  const key = `${srcRate}>${dstRate}`
  if (!designed.has(key)) designed.set(key, designFilter(srcRate, dstRate))
  return designed.get(key)
}

// Keeps whatever input the next output samples still need, and hands out
// every output sample the input so far can make. One per stream: reset()
// when the stream breaks, as when capture pauses, so the next chunk
// doesn't splice onto audio from before the gap.
export class Resampler {
  constructor(srcRate, dstRate = TARGET_RATE) {
    this.srcRate = Math.round(srcRate)
    this.dstRate = Math.round(dstRate)
    if (!(this.srcRate > 0) || !(this.dstRate > 0)) throw new RangeError('rates must be positive')
    this.same = this.srcRate === this.dstRate
    if (!this.same) this.filter = filterFor(this.srcRate, this.dstRate)
    this.buf = new Float64Array(0)
    this.reset()
  }

  // Forget the stream so far. The next chunk starts a fresh one.
  reset() {
    this.started = false
    this.bufStart = 0   // the input index of buf[0]
    this.bufLen = 0
    this.pushed = 0     // input samples since the stream started
    this.emitted = 0    // output samples since the stream started
    this.whole = 0      // the next output sample's input index...
    this.frac = 0       // ...and how far past it, in 1/up steps
  }

  // The output samples this chunk completes, as a Float32Array.
  push(input) {
    const n = input ? input.length : 0
    if (this.same) {
      this.pushed += n
      this.emitted += n
      return Float32Array.from(input || [])
    }
    if (!n) return new Float32Array(0)
    if (!this.started) this._prime(input[0])
    this._append(input, n)
    this.pushed += n
    return this._run(this.bufStart + this.bufLen - 1, Infinity)
  }

  // End of the stream: the output samples still owed, with the last input
  // sample held past the end. A stream of N input samples makes
  // floor(N * dstRate / srcRate) in all.
  flush() {
    if (this.same || !this.started) {
      this.reset()
      return new Float32Array(0)
    }
    const owed = Math.floor((this.pushed * this.dstRate) / this.srcRate)
    const { taps } = this.filter
    const last = this.buf[this.bufLen - 1]
    const pad = new Float64Array(taps).fill(last)
    const out = []
    let total = 0
    while (this.emitted < owed) {
      this._append(pad, taps)
      const part = this._run(this.bufStart + this.bufLen - 1, owed)
      out.push(part)
      total += part.length
    }
    this.reset()
    const all = new Float32Array(total)
    let at = 0
    for (const part of out) { all.set(part, at); at += part.length }
    return all
  }

  // A stream starts as if its first sample had always been there, so the
  // first output samples don't ramp up from silence.
  _prime(first) {
    const { taps } = this.filter
    this.started = true
    this._ensure(taps)
    this.buf.fill(first, 0, taps)
    this.bufStart = -taps
    this.bufLen = taps
  }

  _ensure(size) {
    if (this.buf.length >= size) return
    const bigger = new Float64Array(Math.max(size, this.buf.length * 2))
    bigger.set(this.buf.subarray(0, this.bufLen))
    this.buf = bigger
  }

  _append(input, n) {
    this._ensure(this.bufLen + n)
    this.buf.set(input, this.bufLen)
    this.bufLen += n
  }

  // Every output sample whose last input sample is at or before `lastIndex`,
  // stopping at `limit` in all, then drop the input nothing needs any more.
  _run(lastIndex, limit) {
    const { up, down, phases, taps, rows } = this.filter
    const half = taps / 2
    const stepWhole = Math.floor(down / up)
    const stepFrac = down % up
    const exact = phases === up
    const buf = this.buf
    const room = Math.max(0, Math.floor(((lastIndex - this.whole - half + 1) * up) / down) + 2)
    const out = new Float32Array(room)
    let count = 0
    let whole = this.whole
    let frac = this.frac
    let emitted = this.emitted
    while (whole + half <= lastIndex && emitted < limit) {
      const p = exact ? frac : Math.round((frac * phases) / up)
      const row = p * taps
      // The newest input sample this output uses, in the buffer. Tap j
      // weighs the sample j before it.
      const at = whole + half - this.bufStart
      let acc = 0
      for (let j = 0; j < taps; j++) acc += buf[at - j] * rows[row + j]
      out[count++] = acc
      emitted++
      whole += stepWhole
      frac += stepFrac
      if (frac >= up) { frac -= up; whole++ }
    }
    this.whole = whole
    this.frac = frac
    this.emitted = emitted
    // Keep from the oldest sample the next output sample reads.
    const keepFrom = Math.min(whole + half - taps + 1, this.bufStart + this.bufLen) - this.bufStart
    if (keepFrom > 0) {
      buf.copyWithin(0, keepFrom, this.bufLen)
      this.bufLen -= keepFrom
      this.bufStart += keepFrom
    }
    return count === room ? out : out.subarray(0, count)
  }
}

// A whole recording in one go: what streaming it would give, plus the tail.
export function resample(samples, srcRate, dstRate = TARGET_RATE) {
  const r = new Resampler(srcRate, dstRate)
  const head = r.push(samples)
  const tail = r.flush()
  if (!tail.length) return head
  const all = new Float32Array(head.length + tail.length)
  all.set(head)
  all.set(tail, head.length)
  return all
}
