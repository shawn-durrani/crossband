import { useRef, useState } from 'react'
import { api, streamSSE } from '../api'
import { participantInfo } from '../speakers'
import { mergeGuestJob } from '../guestJobs'
import { eventBelongsToActiveChat } from '../streamGuard'
import { voiceReplaySpeakerEligible } from '../eventStream'
import { createBatch, addFragment, cancelBatch, flipBatch, combineFragments } from '../textQueue'

// The round loop's client side: everything between "the user hit Send" and
// "the round is over" and, deliberately, the STATE that produces, not just
// the behaviour. The transcript, the streaming flag, round progress,
// the per-chat outbound batch and the offline send queue all live here, because
// the round loop is their only writer; App reads them from the hook's return.
//
// Owns: messages, streaming(+ref), roundProgress, the outbound text batch, the
// offline held-sends queue, and the refs only this loop touches (liveIds,
// roundCtrl, tailedRounds, voiceAttachInFlight).
//
// Explicitly NOT owned, told to App through callbacks instead:
//   onRunningMark/onRunningClear - the optimistic running set folds into
//     server truth in App's applyRunning; this loop only knows its own round.
//   onChatDirty - the distill-on-leave bookkeeping belongs with the chat CRUD
//     that consumes it.
//   onRoundSettled - useEventStream's deferred-event drain, late-bound by App
//     because the two hooks reference each other.
//
// Every public function reads the active chat through `activeChatIdRef` at
// call time, never through a captured state value. That is what makes them
// safe to close over from anywhere - the VoiceController is constructed once
// and keeps its first `sendText` forever, which under the old inline version
// silently bound the chat id from that render (harmless only because voice
// stops on chat switch today).
export function useRoundStream({
  activeChatIdRef, stateRef, voiceRef, voiceActiveRef, addCaptionRef,
  refreshState, onBanner, onGuestJob, onActiveChatRefresh,
  onRunningMark, onRunningClear, onChatDirty, onRoundSettled,
}) {
  const [messages, setMessages] = useState([])
  const [streaming, setStreaming] = useState(false)
  // Live mirror of `streaming` for closures that run before React re-renders
  // (runStream's finally, send()). setStreaming is async; this is the truth.
  const streamingRef = useRef(false)
  const [roundProgress, setRoundProgress] = useState(null) // {n, total}
  // Per-chat outbound text batch, keyed by chat so each room keeps its own
  // pending batch across navigation (the per-chat invariant).
  const batchStore = useRef(new Map())
  const [activeBatch, setActiveBatch] = useState(createBatch())
  const [heldSends, setHeldSends] = useState(0)
  // Streaming-message ids per speaker, for delta/end/tool events to target.
  const liveIds = useRef({})
  const roundCtrl = useRef(null)
  // Round ids we've already fed to voice - the one we started (round_start),
  // and any detached round we attached to. Stops a voice-mode re-attach from
  // double-voicing a round, and from re-attaching one the user barged out of.
  const tailedRounds = useRef(new Set())
  const voiceAttachInFlight = useRef(false)
  const pendingSends = useRef([])
  const sendRetryTimer = useRef(null)

  // Callbacks read through a ref refreshed every render, so the long-lived
  // stream loops always call today's handlers (same rule as useEventStream).
  const cb = useRef(null)
  cb.current = {
    refreshState, onBanner, onGuestJob, onActiveChatRefresh,
    onRunningMark, onRunningClear, onChatDirty, onRoundSettled,
  }

  function handleEvent(ev, streamChatId) {
    // WRITE-GUARD (the invariant): drop any event whose stream was opened for a
    // chat that is no longer the active one. A detached round in chat A keeps
    // streaming after we switch to chat B; without this, its
    // deltas/placeholders would paint into chat B's transcript (and speak
    // through voice). stateRef.current.chatId is the live active chat.
    if (!eventBelongsToActiveChat(streamChatId, stateRef.current.chatId)) return
    voiceRef.current?.onEvent(ev)
    if (ev.type === 'round') {
      setRoundProgress({ n: ev.n, total: ev.total })
    } else if (ev.type === 'user_saved') {
      setMessages((m) => [...m, ev.message])
      // Voice: surface the transcribed utterance briefly as a muted "You: …" caption.
      if (voiceActiveRef.current) {
        addCaptionRef.current({ speaker: 'user', you: true, text: ev.message?.content || '' })
      }
    } else if (ev.type === 'speaker_start') {
      // Note: the orb tint follows AUDIO playback (voice.js onSpeaker), not this
      // text event - TTS lags and outlasts the text, so text-driven tinting would
      // drop the colour while the agent is still talking.
      const id = `live-${ev.speaker}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
      liveIds.current[ev.speaker] = id
      setMessages((m) => [...m, { id, speaker: ev.speaker, content: '', streaming: true, attachments: [], created_at: Date.now() / 1000 }])
    } else if (ev.type === 'delta') {
      const id = liveIds.current[ev.speaker]
      setMessages((m) =>
        // Real content arriving retires any work_status chip: the model has
        // started actually answering.
        m.map((msg) => (msg.id === id ? { ...msg, content: msg.content + ev.text, workStatus: null } : msg)),
      )
    } else if (ev.type === 'work_status') {
      // A structured, NEVER-persisted liveness signal (backend/
      // work_status.py), held only on the in-flight message's client-side
      // state, never sent anywhere, and gone the instant real content or a
      // tool result arrives (below) or the round ends (speaker_end replaces
      // the whole message object with the server's persisted version, which
      // never carries this field).
      const id = liveIds.current[ev.speaker]
      setMessages((m) =>
        m.map((msg) => (msg.id === id ? { ...msg, workStatus: { phase: ev.phase, label: ev.label } } : msg)),
      )
    } else if (ev.type === 'speaker_end') {
      const id = liveIds.current[ev.speaker]
      delete liveIds.current[ev.speaker]
      // Voice: the finished reply flashes up as a caption in the mobile overlay.
      if (voiceActiveRef.current) {
        const { label, color } = participantInfo(ev.speaker, stateRef.current.participants)
        addCaptionRef.current({ speaker: ev.speaker, name: label, color, text: ev.message?.content || '' })
      }
      setMessages((m) =>
        m.map((msg) => (msg.id === id ? { ...ev.message, attachments: [] } : msg)),
      )
    } else if (ev.type === 'passed') {
      // #98: the seat chose the honourable silence - the app removes the
      // turn entirely. Nothing persisted server-side; the streamed bubble
      // (its only content was the pass token) goes too. On a refused pass
      // the same event precedes the retry's fresh speaker_start.
      const id = liveIds.current[ev.speaker]
      delete liveIds.current[ev.speaker]
      setMessages((m) => m.filter((msg) => msg.id !== id))
    } else if (ev.type === 'tool_activity') {
      const id = liveIds.current[ev.speaker]
      setMessages((m) =>
        m.map((msg) =>
          msg.id === id
            ? {
                ...msg,
                workStatus: null,
                tool_events: [
                  ...(msg.tool_events || []),
                  { id: `${id}-t${(msg.tool_events || []).length}`, tool: ev.tool, input_json: ev.input_json, output_text: ev.output_text },
                ],
              }
            : msg,
        ),
      )
    } else if (ev.type === 'error') {
      const id = liveIds.current[ev.speaker]
      delete liveIds.current[ev.speaker]
      setMessages((m) =>
        m.map((msg) =>
          msg.id === id ? { ...msg, streaming: false, error: ev.message } : msg,
        ),
      )
    } else if (ev.type === 'guest_job') {
      // A guest was summoned this round: show its chip immediately for
      // the client that sent, without waiting for the global stream to echo it.
      cb.current.onGuestJob((j) => mergeGuestJob(j, ev))
    }
  }

  async function runStream(url, body, { queueable = false } = {}) {
    streamingRef.current = true
    setStreaming(true)
    roundCtrl.current = new AbortController()
    const signal = roundCtrl.current.signal
    const chatId = activeChatIdRef.current
    // Light the running indicator for this chat immediately; the server's
    // running_chat_ids reconciles it (and keeps it lit if the round detaches).
    cb.current.onRunningMark(chatId)
    // Detached rounds: track the round id + how many events we've consumed so
    // a network drop can re-attach and catch up instead of killing the round.
    const track = { roundId: null, count: 0 }
    // Only a round that ends NATURALLY drains the queued batch. Aborts (Stop or
    // navigating away mid-round) leave the batch queued - draining then could
    // start a second round on a chat whose detached round is still running.
    let aborted = false
    const onEvent = (ev) => {
      if (ev.type === 'round_start') {
        track.roundId = ev.round_id
        tailedRounds.current.add(ev.round_id) // never voice-re-attach a round we started
        return
      }
      if (ev.type === 'aborted') return
      track.count += 1
      handleEvent(ev, chatId)
    }
    try {
      try {
        await streamSSE(url, body, onEvent, signal)
      } catch (e) {
        if (e.name === 'AbortError') throw e
        if (track.roundId) {
          // The round is still running server-side - hold on and re-attach.
          await reattachRound(chatId, track, onEvent, signal)
        } else if (queueable && e instanceof TypeError) {
          // The send never reached the server (offline): hold it, retry later.
          pendingSends.current.push({ url, body })
          setHeldSends(pendingSends.current.length)
          cb.current.onBanner('No connection - holding your message; it sends when the tunnel returns.')
          ensureSendRetry()
          return
        } else {
          throw e
        }
      }
      cb.current.onChatDirty(chatId)
    } catch (e) {
      if (e.name === 'AbortError') {
        aborted = true
        cb.current.onChatDirty(chatId) // interrupted rounds still count
      } else {
        cb.current.onBanner(e.message)
      }
    } finally {
      roundCtrl.current = null
      streamingRef.current = false
      setStreaming(false)
      setRoundProgress(null)
      setMessages((m) => m.map((msg) => ({ ...msg, streaming: false })))
      // Surface any out-of-band notices (deploy narration, hand-back rounds)
      // that arrived while this round was streaming and were held back so the
      // watermark wouldn't skip them. Runs even on abort, since those notices
      // are unrelated to the round and must appear regardless.
      cb.current.onRoundSettled()
      // Drain any text the user queued during this round. We decided the open
      // question in favour of draining AFTER the assistant/guest turn
      // completes, not merely after ingestion; this is the reliable
      // post-long-running-task resume path the CC-completion stall needs.
      if (!aborted) drainBatch(chatId)
      // Drop the optimistic flag; refreshState() below reconciles from the
      // server, so a still-detached round stays lit and a finished one clears.
      cb.current.onRunningClear(chatId)
      // NB: don't clear speakingSlug here - the text round is done but the TTS
      // audio is still playing. voice.js onSpeaker(null) clears it when the last
      // reply finishes speaking, so the orb stays coloured for the whole reply.
      voiceRef.current?.onRoundDone()
      cb.current.refreshState()
      // Refresh the round chat's voice/context totals - guarded to the LIVE
      // active chat. The inline version captured activeChatId at send time and
      // repainted it unconditionally, so finishing a round after switching
      // chats could stamp chat A's header data onto a view showing chat B
      // (the cross-chat write bug class; the old reattach guard compared a
      // closure variable to itself, which is always true). Fixed here,
      // deliberately.
      if (chatId && chatId === activeChatIdRef.current) {
        api.getChat(chatId).then((d) => {
          if (chatId === activeChatIdRef.current) cb.current.onActiveChatRefresh(d.chat)
        }).catch(() => {})
      }
    }
  }

  // Voice mode: attach to a DETACHED round this client didn't start - a Claude
  // Code hand-back narration, or a round that began while we were backgrounded -
  // and feed it to the audio pipeline so it's actually SPOKEN. Voice-only
  // on purpose: the text/captions still render via the normal new_message
  // hydration path, so this can't double-paint the transcript. We only ever
  // attach to a round we've NEVER fed to voice (guarded by tailedRounds), so
  // tailing from the buffer start (after=0) voices the whole narration exactly
  // once - nothing was spoken yet.
  async function voiceAttachRound(chatId, speaker) {
    if (voiceAttachInFlight.current || streamingRef.current) return
    voiceAttachInFlight.current = true
    try {
      const { round_id, last_round_id } =
        await api.activeRound(chatId).catch(() => ({}))
      // #64: a single-narrator hand-back finishes its round with the very
      // message whose event brought us here, so the ACTIVE id is usually
      // already null. The last round's buffer outlives completion; replaying
      // it is what turns the narration into speech. Two guards keep that
      // safe: tailedRounds makes each round voice at most once (a round we
      // streamed live is never replayed), and the replay path only fires for
      // a message a round could have produced - a participant's turn, per
      // voiceReplaySpeakerEligible - so a deploy notice or a guest job
      // result can never resurrect an old round aloud.
      const target = round_id
        || (voiceReplaySpeakerEligible(speaker) ? last_round_id : null)
      // Re-check the world after the await: still this chat, still in voice, not
      // a round we already fed, and not one we started/aborted out of.
      if (chatId !== activeChatIdRef.current || !voiceActiveRef.current) return
      if (streamingRef.current || !target || tailedRounds.current.has(target)) return
      tailedRounds.current.add(target)
      const ctrl = new AbortController()
      try {
        await streamSSE(
          `/api/chats/${chatId}/round/stream?round_id=${target}&after=0`,
          null,
          (ev) => { if (chatId === activeChatIdRef.current) voiceRef.current?.onEvent(ev) },
          ctrl.signal, 'GET')
      } catch { /* dropped/aborted - messages persisted regardless */ }
      voiceRef.current?.onRoundDone() // clears roundActive/dropQueue for the next turn
    } finally {
      voiceAttachInFlight.current = false
    }
  }

  // A round drop mid-stream: the server keeps generating; we re-attach with
  // backoff and replay what we missed. Ends when the stream completes, the
  // user aborts, or the round is gone (finished while away → refetch the chat).
  async function reattachRound(chatId, track, onEvent, signal) {
    cb.current.onBanner('Connection lost - the models are still working; reconnecting…')
    for (let delay = 2000; ; delay = Math.min(8000, delay + 1500)) {
      await new Promise((r) => setTimeout(r, delay))
      if (signal.aborted) { const e = new Error('aborted'); e.name = 'AbortError'; throw e }
      try {
        await streamSSE(
          `/api/chats/${chatId}/round/stream?round_id=${track.roundId}&after=${track.count}`,
          null, onEvent, signal, 'GET')
        cb.current.onBanner('')
        return
      } catch (e) {
        if (e.name === 'AbortError') throw e
        if (/no such round/i.test(e.message)) {
          // finished while we were away - everything persisted server-side
          const d = await api.getChat(chatId).catch(() => null)
          if (d && chatId === activeChatIdRef.current) {
            cb.current.onActiveChatRefresh(d.chat)
            setMessages(d.messages)
          }
          cb.current.onBanner('')
          return
        }
        // still offline - loop and try again
      }
    }
  }

  // Held sends (offline queue): retry in order whenever the tunnel returns.
  function ensureSendRetry() {
    if (sendRetryTimer.current) return
    const tick = async () => {
      sendRetryTimer.current = null
      if (!pendingSends.current.length) { setHeldSends(0); return }
      if (await api.ping()) {
        const item = pendingSends.current.shift()
        setHeldSends(pendingSends.current.length)
        if (!pendingSends.current.length) cb.current.onBanner('')
        await runStream(item.url, item.body, { queueable: true })
      }
      if (pendingSends.current.length) sendRetryTimer.current = setTimeout(tick, 3000)
      else setHeldSends(0)
    }
    sendRetryTimer.current = setTimeout(tick, 3000)
  }

  // ---- Per-chat text batching --------------------------------------------
  const getBatch = (chatId) => batchStore.current.get(chatId) || createBatch()
  const putBatch = (chatId, batch) => {
    batchStore.current.set(chatId, batch)
    if (chatId === stateRef.current.chatId) setActiveBatch(batch)
  }

  // Add a typed message to this chat's pending batch. Called by send() while a
  // round is in flight; the batch drains when the round finishes.
  function queueText(chatId, text, attachments) {
    putBatch(chatId, addFragment(getBatch(chatId), text, Date.now(), attachments.map((a) => a.id)))
  }

  // Cancel the active chat's pending batch - allowed only before the atomic
  // flip. The pure state machine no-ops if it's already committed.
  function cancelActiveBatch() {
    const chatId = stateRef.current.chatId
    if (!chatId) return
    putBatch(chatId, cancelBatch(getBatch(chatId)))
  }

  // Hand the queued batch to the backend as ONE user turn. The queued→in-flight
  // flip is the single atomic decision (see textQueue.js): a cancel that landed
  // first already emptied the batch, so committed is false and we send nothing.
  function drainBatch(chatId) {
    if (!chatId || streamingRef.current) return
    const current = getBatch(chatId)
    const { batch: flipped, committed } = flipBatch(current)
    if (!committed) return
    putBatch(chatId, flipped)
    const text = combineFragments(current)
    const attachment_ids = current.attachmentIds
    putBatch(chatId, createBatch()) // batch consumed; reset to empty
    runStream(`/api/chats/${chatId}/send`, { text, attachment_ids }, { queueable: true })
  }

  // Restore / discard a chat's pending batch as the user navigates - called by
  // App's selectChat and deleteChat, which own navigation.
  function restoreBatchFor(chatId) { setActiveBatch(getBatch(chatId)) }
  function dropBatchFor(chatId) { batchStore.current.delete(chatId) }

  function send(text, attachments, { batch = true, turnId } = {}) {
    const chatId = activeChatIdRef.current
    if (!chatId) return
    // A round is already running for this chat → queue the text and batch it;
    // the composer stays live so follow-ups don't race the wire or get lost.
    // Voice (batch:false) is exempt: its interrupt/barge-in semantics own turn
    // timing and must stay unchanged (batching is text-only).
    if (batch && streamingRef.current) {
      queueText(chatId, text, attachments)
      return
    }
    runStream(`/api/chats/${chatId}/send`, {
      text,
      attachment_ids: attachments.map((a) => a.id),
      // Correlates this turn with the client-side voice-trace stages
      // (frontend/src/voiceTrace.js) so the server can log its own
      // context-assembly/provider-TTFT split against the SAME turn_id.
      // Always undefined for text sends - JSON.stringify drops it, so the
      // backend sees no turn_id and records nothing extra.
      turn_id: turnId,
    }, { queueable: true })
    // Slash commands go to machine-side tooling, which replies asynchronously
    // with a System notice; the global live-events stream surfaces that (and
    // any other out-of-band insert); see hooks/useEventStream.js.
  }

  function continueRound(rounds) {
    const chatId = activeChatIdRef.current
    if (!chatId) return
    runStream(`/api/chats/${chatId}/continue`, { rounds })
  }

  function stopRound() {
    // Detached rounds don't die with the connection - stopping is explicit.
    const chatId = activeChatIdRef.current
    if (chatId) api.abortRound(chatId).catch(() => {})
    roundCtrl.current?.abort()
  }

  // Tear down the LIVE CLIENT-SIDE view of the current chat before navigating
  // away. This is hygiene layered on top of the write-guard: it stops the old
  // stream's reader loop and clears the live UI so nothing keeps ticking.
  //
  // CRITICAL: this aborts only our reader/stream + local UI + voice - it must
  // NOT abort the backend round. Chat A's detached round keeps running
  // server-side, finishes, and persists; we catch up via reattachRound when the
  // user returns. Never call api.abortRound() here (that's for deliberate
  // barge-in only) or detached rounds break.
  function teardownLiveView() {
    roundCtrl.current?.abort()
    voiceRef.current?.stop() // voice is single-active-room; kill chat A's TTS
    liveIds.current = {}
    setStreaming(false)
    setRoundProgress(null)
  }

  return {
    messages, setMessages, streaming, streamingRef, roundProgress,
    activeBatch, heldSends,
    send, continueRound, stopRound, voiceAttachRound,
    cancelActiveBatch, restoreBatchFor, dropBatchFor,
    teardownLiveView,
  }
}
