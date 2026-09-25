// The held-sends retry. A message that couldn't go (no connection, or a
// round still finishing, #434) waits in `queue`, and this sends the held
// messages in order, one at a time, every few seconds until none are left.
//
// One retry runs at a time. The loop this replaced cleared its timer when
// a retry began, so a message held during that retry scheduled a second
// one, and the end of the first retry scheduled a third over it. Retries
// multiplied, and two that checked the connection at the same moment both
// saw the last held message: one sent it, and the other took from an empty
// queue and threw "undefined is not an object (evaluating 'e.url')", seen
// in the voice diagnostics of 25 September.
export const SEND_RETRY_MS = 3000

// `ping()` resolves true when the server answers. `send(item)` sends one
// held message; a send that's turned away again puts it back in the queue
// and calls ensure(). `onCount(n)` reports how many are still held, and
// `onEmptied()` runs when the last one leaves the queue.
export function createSendRetry({ queue, ping, send, onCount, onEmptied, delayMs = SEND_RETRY_MS }) {
  let timer = null
  let running = false

  async function tick() {
    timer = null
    running = true
    try {
      if (!queue.length || !(await ping())) return
      const item = queue.shift()
      onCount(queue.length)
      if (!queue.length) onEmptied()
      await send(item)
    } finally {
      running = false
      if (queue.length) ensure()
      else onCount(0)
    }
  }

  // Start retrying unless a retry is already waiting or running. A running
  // one schedules the next itself when it's done.
  function ensure() {
    if (timer || running) return
    timer = setTimeout(tick, delayMs)
  }

  return { ensure }
}
