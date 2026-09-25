// The held-sends retry: messages held offline, or while a round finishes
// (#434), go out in order, each once, and only one retry runs at a time.
// The loop this replaced let retries multiply: a message held while a
// retry was checking the connection started a second retry, and two
// retries that both saw the last held message raced for it. The loser
// took from an empty queue and threw "undefined is not an object
// (evaluating 'e.url')", which the voice diagnostics of 25 September
// caught as an unhandled rejection.
// Run: node --test frontend/src/sendRetry.test.js
import assert from 'node:assert/strict'
import { afterEach, beforeEach, mock, test } from 'node:test'
import { SEND_RETRY_MS, createSendRetry } from './sendRetry.js'

const PING_MS = 200

beforeEach(() => { mock.timers.enable({ apis: ['setTimeout'] }) })
afterEach(() => { mock.timers.reset() })

// A retry over a real queue. The connection check takes PING_MS and
// answers `online`; a send the server turns away (`turnAway`, once per
// url) goes back in the queue, as runStream does on a 409.
function harness() {
  const queue = []
  const h = {
    queue,
    online: true,
    turnAway: new Set(),
    sent: [],
    counts: [],
    emptied: 0,
    pingsInFlight: 0,
    mostPingsAtOnce: 0,
    hold(url) { queue.push({ url, body: {} }); h.retry.ensure() },
  }
  h.retry = createSendRetry({
    queue,
    ping: () => {
      h.pingsInFlight++
      h.mostPingsAtOnce = Math.max(h.mostPingsAtOnce, h.pingsInFlight)
      return new Promise((resolve) => setTimeout(() => {
        h.pingsInFlight--
        resolve(h.online)
      }, PING_MS))
    },
    send: async (item) => {
      h.sent.push(item.url)   // an empty slot would throw right here
      if (h.turnAway.delete(item.url)) h.hold(item.url)
    },
    onCount: (n) => h.counts.push(n),
    onEmptied: () => { h.emptied++ },
  })
  return h
}

async function advance(ms, step = 50) {
  for (let t = 0; t < ms; t += step) {
    mock.timers.tick(step)
    for (let i = 0; i < 4; i++) await new Promise((r) => setImmediate(r))
  }
}

test('held messages go in order, each once, a few seconds apart', async () => {
  const h = harness()
  h.hold('/send/a')
  h.hold('/send/b')
  await advance(SEND_RETRY_MS - 100)
  assert.deepEqual(h.sent, [], 'nothing before the first retry is due')
  await advance(3 * SEND_RETRY_MS)
  assert.deepEqual(h.sent, ['/send/a', '/send/b'])
  assert.equal(h.queue.length, 0)
  assert.equal(h.emptied, 1)
  assert.equal(h.counts.at(-1), 0)
})

test('a message held while a retry checks the connection starts no second retry', async () => {
  const h = harness()
  // The first held message is turned away once more, as a 409 would be
  // while the round is still finishing.
  h.turnAway.add('/send/a')
  h.hold('/send/a')
  await advance(SEND_RETRY_MS + PING_MS / 2)
  assert.equal(h.pingsInFlight, 1, 'a retry is checking the connection')
  // Another message is held right then.
  h.hold('/send/b')
  await advance(10 * SEND_RETRY_MS)
  assert.equal(h.mostPingsAtOnce, 1, 'one retry at a time')
  assert.deepEqual(h.sent, ['/send/a', '/send/b', '/send/a'],
                   'every held message went, and none from an empty slot')
  assert.equal(h.queue.length, 0)
  assert.equal(h.counts.at(-1), 0)
})

test('while offline the retry keeps checking, then sends once back online', async () => {
  const h = harness()
  h.online = false
  h.hold('/send/a')
  await advance(4 * SEND_RETRY_MS)
  assert.deepEqual(h.sent, [])
  assert.equal(h.queue.length, 1, 'the message is still held')
  h.online = true
  h.hold('/send/b')
  await advance(4 * SEND_RETRY_MS)
  assert.deepEqual(h.sent, ['/send/a', '/send/b'])
  assert.equal(h.mostPingsAtOnce, 1)
})

test('a retry that finds the queue empty stops and reports none held', async () => {
  const h = harness()
  h.retry.ensure()
  await advance(2 * SEND_RETRY_MS)
  assert.deepEqual(h.counts, [0])
  assert.equal(h.mostPingsAtOnce, 0, 'nothing to send, so no connection check')
})
