#!/usr/bin/env node
// What resampling the mic to 16 kHz costs (#505): the time per mic
// callback of 4096 samples, as a share of the audio it carries, and the
// time for a whole turn's identity copy. Run it on the machine you care
// about: node frontend/scripts/resample-bench.mjs
import { Resampler, designFilter, resample } from '../src/resample.js'

const CHUNK = 4096
const ROUNDS = 2000

function speechLike(n, rate) {
  const x = new Float32Array(n)
  for (let i = 0; i < n; i++) {
    x[i] = 0.3 * Math.sin((2 * Math.PI * 220 * i) / rate) + 0.02 * Math.sin(i * 1.7)
  }
  return x
}

for (const rate of [48000, 44100]) {
  const r = new Resampler(rate)
  const chunk = speechLike(CHUNK, rate)
  for (let i = 0; i < 200; i++) r.push(chunk)   // let the JIT settle
  const start = process.hrtime.bigint()
  for (let i = 0; i < ROUNDS; i++) r.push(chunk)
  const perChunk = Number(process.hrtime.bigint() - start) / 1e6 / ROUNDS
  const audioMs = (CHUNK / rate) * 1000
  const f = designFilter(rate)
  console.log(`${rate} Hz: ${perChunk.toFixed(3)} ms per ${CHUNK}-sample callback ` +
              `(${audioMs.toFixed(1)} ms of audio, ${((perChunk / audioMs) * 100).toFixed(2)}%), ` +
              `${f.taps} taps, ${f.phases} phase rows`)
  for (const seconds of [10, 120]) {
    const clip = speechLike(rate * seconds, rate)
    const t = process.hrtime.bigint()
    resample(clip, rate)
    const ms = Number(process.hrtime.bigint() - t) / 1e6
    console.log(`  a ${seconds} s identity copy: ${ms.toFixed(1)} ms`)
  }
}
