// Record someone's voice on purpose (#504), inside their row on the Voices
// page. The rules and words live in ../voiceRecording.js (pure,
// node-tested). VoiceRecorderView is markup only, so the render smoke can
// draw every phase; the default export wires the microphone.
//
// The capture uses the voice chat's own mic setting (../captureProfile.js)
// and the app's own 16 kHz WAV helper (../identityClip.js), so a recording
// here sounds like a turn in a voice chat, and both follow any change to
// either. The audio goes to this app's server and nowhere else.
import { useEffect, useRef, useState } from 'react'
import { Mic, Square, X } from 'lucide-react'

import { api } from '../api.js'
import { captureConstraints } from '../captureProfile.js'
import { identityWav } from '../identityClip.js'
import {
  IDLE, PASSAGES, PASSAGE_STORE_KEY, READINESS_GIVE_UP_MS, READINESS_POLL_MS,
  joinChunks, lastPassage, liveMicWarning, meterLevel, micErrorMessage,
  nextPassageIndex, recordOutcome, recordStep, rememberPassage, savedLine,
  timerLine,
} from '../voiceRecording.js'

// Which passage each person read last: a per-browser convenience. Storage
// that throws or comes back empty just starts at the first passage.
function readMemory() {
  try {
    return JSON.parse(localStorage.getItem(PASSAGE_STORE_KEY) || '{}') || {}
  } catch {
    return {}
  }
}

function writeMemory(memory) {
  try { localStorage.setItem(PASSAGE_STORE_KEY, JSON.stringify(memory)) } catch { /* */ }
}

const BUTTON = 'inline-flex items-center gap-1 border border-edge rounded px-2 py-1 '
  + 'text-ink-dim hover:text-ink disabled:opacity-40'

export function VoiceRecorderView({ name, passage, state, onStart, onStop, onClose }) {
  const phase = (state && state.phase) || 'idle'
  const busy = phase === 'starting' || phase === 'saving' || phase === 'checking'
  const outcome = phase === 'done' ? recordOutcome(state) : null
  const level = Math.round(((state && state.level) || 0) * 100)
  const startLabel = phase === 'done' ? 'Record again'
    : ['refused', 'blocked', 'failed'].includes(phase) ? 'Try again' : 'Start recording'
  return (
    <div className="mt-1.5 border border-edge2 rounded-lg px-3 py-2 space-y-2 text-xs">
      <div className="text-ink-mid">
        {phase === 'done' ? 'To record again, ask' : 'Ask'} {name} to sit at
        the microphone and read this aloud at an easy pace. It takes about 30
        seconds.
      </div>
      <blockquote className="border-l-2 border-edge pl-2 text-sm text-ink leading-relaxed">
        {passage}
      </blockquote>
      <div aria-live="polite" className="space-y-1">
        {phase === 'starting' && (
          <div className="text-ink-dim">Starting the microphone…</div>
        )}
        {phase === 'recording' && (
          <>
            <div
              className="h-2 w-full max-w-xs rounded-full bg-panel2 overflow-hidden"
              role="meter"
              aria-label="Microphone level"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={level}
            >
              <div className="h-full rounded-full bg-emerald-600"
                   style={{ width: `${level}%` }} />
            </div>
            <div className="text-ink-dim tabular-nums">{timerLine(state.elapsedMs)}</div>
          </>
        )}
        {phase === 'saving' && (
          <div className="text-ink-dim">
            {state.auto ? 'Stopped at 45 seconds. ' : ''}Saving…
          </div>
        )}
        {phase === 'checking' && (
          <div className="text-ink-dim">
            {savedLine(state.answer)} Checking whether the voice is ready…
          </div>
        )}
        {phase === 'done' && (
          <>
            <div className="text-ink-mid">{savedLine(state.answer)}</div>
            {outcome.text && (
              <div className={outcome.ready ? 'text-emerald-300' : 'text-ink'}>
                {outcome.text}
              </div>
            )}
            {outcome.advice && <div className="text-ink-dim">{outcome.advice}</div>}
          </>
        )}
        {['refused', 'blocked', 'failed'].includes(phase) && (
          <div className="text-amber-300/90">{state.message}</div>
        )}
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        {phase === 'recording' ? (
          <button className={BUTTON} onClick={onStop}>
            <Square size={11} /> Stop and save
          </button>
        ) : (
          <button className={BUTTON} disabled={busy} onClick={onStart}>
            <Mic size={11} /> {startLabel}
          </button>
        )}
        <button className="inline-flex items-center gap-1 text-ink-dim hover:text-ink px-1"
                onClick={onClose}>
          <X size={11} />
          {phase === 'recording' || phase === 'starting' ? 'Cancel' : 'Close'}
        </button>
      </div>
    </div>
  )
}

export default function VoiceRecorder({ personId, name, onSaved, onClose }) {
  const [passageIndex, setPassageIndex] = useState(
    () => nextPassageIndex(lastPassage(readMemory(), personId)))
  const passageRef = useRef(passageIndex)
  const [state, setState] = useState(IDLE)
  const stateRef = useRef(IDLE)
  const mic = useRef(null)
  const poll = useRef(null)
  const alive = useRef(true)

  function dispatch(event) {
    const next = recordStep(stateRef.current, event)
    stateRef.current = next
    if (alive.current) setState(next)
    return next
  }

  function choosePassage(index) {
    passageRef.current = index
    setPassageIndex(index)
  }

  // Stop the microphone and hand back what it heard. Safe to call twice.
  function releaseMic() {
    const m = mic.current
    mic.current = null
    if (!m) return null
    clearInterval(m.timer)
    try {
      m.proc.onaudioprocess = null
      m.src.disconnect()
      m.proc.disconnect()
    } catch { /* */ }
    m.stream.getTracks().forEach((t) => t.stop())
    m.ctx.close().catch(() => {})
    return m
  }

  function stopPolling() {
    clearTimeout(poll.current)
    poll.current = null
  }

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      stopPolling()
      releaseMic()
    }
  }, [])

  async function start() {
    if (dispatch({ type: 'start' }).phase !== 'starting') return
    const Ctx = window.AudioContext || window.webkitAudioContext
    if (!Ctx || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      dispatch({ type: 'mic_failed', message: 'This browser can\'t record audio here.' })
      return
    }
    // Made inside the click, so a phone lets it run.
    const ctx = new Ctx()
    try {
      const live = await api.listCaptures()
      const warning = liveMicWarning(live && live.captures)
      if (warning) {
        ctx.close().catch(() => {})
        dispatch({ type: 'blocked', message: warning })
        return
      }
    } catch { /* can't ask: the recording goes ahead */ }
    let stream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: captureConstraints(true) })
    } catch (e) {
      ctx.close().catch(() => {})
      dispatch({ type: 'mic_failed', message: micErrorMessage(e) })
      return
    }
    if (stateRef.current.phase !== 'starting') {
      // Cancelled while the browser was asking for the microphone.
      stream.getTracks().forEach((t) => t.stop())
      ctx.close().catch(() => {})
      return
    }
    ctx.resume().catch(() => {})
    const src = ctx.createMediaStreamSource(stream)
    const proc = ctx.createScriptProcessor(4096, 1, 1)
    const m = { ctx, stream, src, proc, chunks: [], rate: ctx.sampleRate, level: 0,
                startedAt: Date.now(), timer: null }
    proc.onaudioprocess = (ev) => {
      const data = ev.inputBuffer.getChannelData(0)
      m.chunks.push(new Float32Array(data))
      m.level = meterLevel(data)
    }
    src.connect(proc)
    proc.connect(ctx.destination)
    mic.current = m
    dispatch({ type: 'mic_ready' })
    m.timer = setInterval(() => {
      const next = dispatch({ type: 'tick', elapsedMs: Date.now() - m.startedAt, level: m.level })
      if (next.phase === 'saving') save()
    }, 200)
  }

  function stop() {
    const next = dispatch({ type: 'stop' })
    if (next.phase === 'saving') save()
    else releaseMic()
  }

  async function save() {
    const m = releaseMic()
    if (!m) return
    const samples = joinChunks(m.chunks)
    const keepMs = Math.round((samples.length / m.rate) * 1000)
    const wav = identityWav(samples, m.rate, { keepMs, dropMs: 0 })
    if (!wav) {
      dispatch({ type: 'refused', message: 'Nothing was recorded. Try again.' })
      return
    }
    let answer
    try {
      answer = await api.recordVoice(personId, wav)
    } catch (e) {
      dispatch({ type: 'refused', message: e.message || 'The recording couldn\'t be saved.' })
      return
    }
    // The next recording of this person gets the next passage.
    writeMemory(rememberPassage(readMemory(), personId, passageRef.current))
    choosePassage(nextPassageIndex(passageRef.current))
    const next = dispatch({ type: 'saved', answer })
    if (onSaved) onSaved()
    if (next.phase === 'checking') pollReadiness(Date.now())
  }

  // Ask again every couple of seconds until the background build covers
  // the new clips, then refresh the page's own line too.
  function pollReadiness(since) {
    stopPolling()
    poll.current = setTimeout(async () => {
      if (stateRef.current.phase !== 'checking') return
      let payload = null
      try { payload = await api.voiceReadiness(personId) } catch { /* next time */ }
      const next = payload ? dispatch({ type: 'readiness', payload }) : stateRef.current
      if (next.phase === 'done') {
        if (onSaved) onSaved()
        return
      }
      if (next.phase !== 'checking') return
      if (Date.now() - since >= READINESS_GIVE_UP_MS) {
        dispatch({ type: 'give_up' })
        return
      }
      pollReadiness(since)
    }, READINESS_POLL_MS)
  }

  function close() {
    stopPolling()
    releaseMic()
    dispatch({ type: 'cancel' })
    if (onClose) onClose()
  }

  return (
    <VoiceRecorderView
      name={name}
      passage={PASSAGES[passageIndex] || PASSAGES[0]}
      state={state}
      onStart={start}
      onStop={stop}
      onClose={close}
    />
  )
}
