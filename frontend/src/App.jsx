import { useEffect, useRef, useState } from 'react'
import { api } from './api'
import { eraseLink } from './voiceDiscard.js'
import { voiceSurface } from './voiceSurface.js'
import { captureBanner } from './micRegistry.js'
import Sidebar from './components/Sidebar'
import Composer from './components/Composer'
import ProjectModal from './components/ProjectModal'
import ModelsPage from './components/ModelsPage'
import ImportModal from './components/ImportModal'
import ExportModal from './components/ExportModal'
import SpendPage from './components/SpendPage'
import VoicesPage from './components/VoicesPage'
import IntegrationsConsole from './components/IntegrationsConsole'
import SetupWizard from './components/SetupWizard'
import MobileVoiceCall from './components/MobileVoiceCall'
import ChatHeader from './components/ChatHeader'
import ThreadView from './components/ThreadView'
import VoiceDock from './components/VoiceDock'
import VoiceStrip from './components/VoiceStrip'
import VoiceController from './voice'
import { useCaptions } from './captions'
import { participantInfo } from './speakers'
import { pendingCount } from './textQueue'
import { computeRunningChats, isChatRunning, shouldPollRunning } from './runningState'
import { resolveHeaderTitle } from './headerState'
import { contextGauge } from './headerView'
import { useEventStream } from './hooks/useEventStream'
import { useRoundStream } from './hooks/useRoundStream'
import { hasVisibleJob } from './guestJobs'
import { adoptRoomMode, askFlag, flagCopy, mergeFlag, mismatchByMessage, rosterChipText, rosterTitle } from './roomState'
import { healthStrip } from './voiceHealth'
import { mergeMessagesById } from './eventStream'
import GuestStatusChip from './components/GuestStatusChip'
import { X, PanelLeft, Plus, AlertTriangle } from 'lucide-react'

const EXAMPLE_PROMPTS = [
  'Pitch me three weekend project ideas, then critique each other’s picks.',
  'Debate: is this a good plan? One of you argue for, one against.',
  'Explain the same concept two ways - one for a child, one for an expert.',
]

export default function App() {
  const [state, setState] = useState({ projects: [], chats: [], participants: [], config: null })
  const [activeChatId, setActiveChatId] = useState(null)
  // Live mirror for async closures (notice polling) - same idea as streamingRef
  const activeChatIdRef = useRef(null)
  useEffect(() => { activeChatIdRef.current = activeChatId }, [activeChatId])
  const [activeChat, setActiveChat] = useState(null)
  // Claude Code guest jobs for the OPEN chat: the collapsed status chip's
  // state, seeded on open and merged live off the global events stream - the
  // same channel messages ride, so voice and text/mobile stay in sync.
  const [guestJobs, setGuestJobs] = useState([])
  const [copiedChat, setCopiedChat] = useState(false)
  // Per-chat running-task state: which chats have a round/agent
  // generating right now - including DETACHED rounds in chats you're not looking
  // at. Source of truth is the backend's running_chat_ids; `optimisticRunning`
  // holds ids for rounds we just kicked off so the indicator lights up before
  // the next poll confirms it.
  const [runningChats, setRunningChats] = useState(() => new Set())
  const optimisticRunning = useRef(new Set())
  // Global live-events bus - one persistent connection per tab,
  // not per chat (see backend/events.py + frontend/src/eventStream.js).
  // Chats with a message that arrived live while some OTHER chat was open -
  // sidebar-only signal, cleared the moment that chat is opened.
  const [unreadChats, setUnreadChats] = useState(() => new Set())
  const [projectModal, setProjectModal] = useState(null)
  const [showParticipants, setShowParticipants] = useState(false)
  const [showVoices, setShowVoices] = useState(false)  // #91: the Voices page
  // Why the Models page was opened: {action:'add'} | {action:'edit',
  // slug}, plus from:'connections' when the console sent you - so the page
  // can land where the click promised and its back button can return you to
  // where you actually came from instead of dumping you in the chat.
  const [modelsIntent, setModelsIntent] = useState(null)
  // Integrations console: the steady-state, full-page operational surface.
  // Rendered in place of the chat pane so the sidebar stays as the nav anchor.
  const [showIntegrations, setShowIntegrations] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(() => localStorage.getItem('sidebar') !== '0')
  const [drawerOpen, setDrawerOpen] = useState(false) // mobile off-canvas sidebar
  const [showImport, setShowImport] = useState(false)
  const [showExport, setShowExport] = useState(false)
  const [showCost, setShowCost] = useState(false)

  // Every full-page surface that replaces the chat pane, as [open, close]
  // pairs. Adding a page means adding ONE row here - pageOpen (the voice
  // strip/call switch) and leavePages both derive from it, so a new page
  // cannot be missed by one of them (the old bug: selectChat cleared one
  // flag of three and clicking a sidebar chat left Spend on screen).
  // modelsIntent is intent metadata for the Models page, not a page flag:
  // it never opens a page on its own, so it stays out of pageOpen and gets
  // its own clear in leavePages.
  const pageFlags = [
    [showIntegrations, setShowIntegrations],
    [showCost, setShowCost],
    [showParticipants, setShowParticipants],
    [showVoices, setShowVoices],
  ]
  const pageOpen = pageFlags.some(([open]) => open)
  const [showSetup, setShowSetup] = useState(false)
  const setupAutoOpened = useRef(false)
  const [banner, setBanner] = useState(null)
  const [theme, setTheme] = useState(() => localStorage.getItem('theme') || 'dark')
  const [voiceState, setVoiceState] = useState('off')
  // Live partial transcript of what the user is SAYING right now -
  // realtime STT only; batch fallback never emits one. Cleared the moment the
  // final transcript dispatches (sendText below) and on session end.
  const [voicePartial, setVoicePartial] = useState(null)
  const [speakingSlug, setSpeakingSlug] = useState(null) // who the orb is tinted to while speaking
  // Transient captions for the mobile voice-call overlay - fed from the SSE
  // event flow below, active only during a live session (UI-only; see captions.js).
  const { captions, history: captionHistory, add: addCaption } =
    useCaptions({ enabled: voiceState !== 'off' })
  const [pttMode, setPttMode] = useState(() => localStorage.getItem('voice_ptt') === '1')
  const [silenceSecs, setSilenceSecs] = useState(() => Number(localStorage.getItem('voice_silence_secs')) || 2.0)
  const [voiceRate, setVoiceRate] = useState(() => Number(localStorage.getItem('voice_rate')) || 1.0)
  const [voiceDockOpen, setVoiceDockOpen] = useState(() => localStorage.getItem('voice_dock_open') !== '0')
  // #67: mute is APP state, not per-surface state - the dock, the mobile
  // call screen and the page strip must all show the same truth about
  // whether the mic is live.
  const [voiceMuted, setVoiceMuted] = useState(false)
  // #134: every live capture session the relay knows about - polled so a
  // microphone left running in ANOTHER window is visible (and killable)
  // from this one.
  const [captures, setCaptures] = useState([])
  useEffect(() => { localStorage.setItem('voice_dock_open', voiceDockOpen ? '1' : '0') }, [voiceDockOpen])
  // Room mode (#28 phase 1): per-SESSION, deliberately NOT persisted - it
  // roughly doubles voice spend while on, so every session starts off and the
  // choice is made knowingly each time (startVoice resets it).
  const [roomMode, setRoomMode] = useState(false)
  // Who last set the session's room-mode flag (#28 room commands): 'server'
  // (seeded from the chat at session start, or adopted from a server-side
  // arm) or 'manual' (the user's own session-only toggle). adoptRoomMode
  // (roomState.js) reads this so a spoken "solo mode" can switch an adopted
  // session off without ever overriding a hand-set session-only toggle.
  const roomModeSourceRef = useRef('server')
  // Room-mode roster/flags snapshot for the OPEN chat (#28 phase 2), plus
  // the remembered-voices list the correction menu offers. Seeded on chat
  // open, refetched on every content-free room_roster/room_flag live event.
  const [roomInfo, setRoomInfo] = useState(null)
  const [voicePeople, setVoicePeople] = useState([])
  // The voice health strip's content-free snapshot (#28): matcher state,
  // mode flags and the last identification's path/latency. Fetched on
  // session start and refreshed off the existing live events (room events
  // and label writes); the strip itself is derived in voiceHealth.js.
  const [voiceHealth, setVoiceHealth] = useState(null)
  const healthFetchedAt = useRef(0)
  const [contRounds, setContRounds] = useState(1)
  const [atBottom, setAtBottom] = useState(true)
  const [newCount, setNewCount] = useState(0) // messages arrived while scrolled up
  const [draft, setDraft] = useState(null) // {text} - prefill the composer (empty-state chips)
  const atBottomRef = useRef(true)
  const prevLenRef = useRef(0)
  const voiceRef = useRef(null)
  const voiceActiveRef = useRef(false) // the round hook's handlers close over stale voiceState; read live here
  voiceActiveRef.current = voiceState !== 'off'
  const addCaptionRef = useRef(addCaption) // ditto for the caption pusher
  addCaptionRef.current = addCaption
  const stateRef = useRef({ chatId: null, participants: [] })
  const dirtyChats = useRef(new Set())
  const [heldVoice, setHeldVoice] = useState(0)
  const scrollRef = useRef(null)

  async function refreshState() {
    const s = await api.state()
    setState(s)
    applyRunning(s.running_chat_ids)
    if (s.memory_writes?.failed?.length) {
      setBanner(`A memory save didn't complete for ${s.memory_writes.failed.length} chat(s) - re-open the chat to retry.`)
    }
    return s
  }

  // Fold the server's running list together with our optimistic ids.
  function applyRunning(serverIds) {
    const set = computeRunningChats(serverIds)
    optimisticRunning.current.forEach((id) => set.add(id))
    setRunningChats(set)
  }

  // While anything is running, poll so a background chat's indicator clears the
  // moment its detached round finishes - then stop polling once all idle.
  useEffect(() => {
    if (!shouldPollRunning(runningChats)) return
    const t = setInterval(() => { refreshState().catch(() => {}) }, 3000)
    return () => clearInterval(t)
  }, [runningChats])

  // The round loop's client side ( step 4): the hook OWNS the transcript,
  // streaming flag, round progress, the batch and the offline send queue -
  // App reads them from its return. The two hooks reference each other
  // (voiceAttachRound <-> the deferred-event drain), so the drain is
  // late-bound through a ref set right after useEventStream returns.
  const drainRef = useRef(() => {})
  const {
    messages, setMessages, streaming, streamingRef, roundProgress,
    activeBatch, heldSends,
    send, continueRound, stopRound, voiceAttachRound,
    cancelActiveBatch, restoreBatchFor, dropBatchFor,
    teardownLiveView,
  } = useRoundStream({
    activeChatIdRef,
    stateRef,
    voiceRef,
    voiceActiveRef,
    addCaptionRef,
    refreshState,
    onBanner: setBanner,
    onGuestJob: setGuestJobs,
    onActiveChatRefresh: setActiveChat,
    // Optimistic running: mark immediately, clear on settle; App's
    // applyRunning folds these with the server's running_chat_ids.
    onRunningMark: (id) => {
      optimisticRunning.current.add(id)
      setRunningChats((prev) => new Set(prev).add(id))
    },
    onRunningClear: (id) => optimisticRunning.current.delete(id),
    onChatDirty: (id) => dirtyChats.current.add(id),
    onRoundSettled: () => drainRef.current(),
  })

  // The global live-events connection now lives in one hook ( step
  // 3): the watermark, reconnect/backoff, the deferred-event queue and the
  // fallback poll are all its business. It hands back the drain function
  // because the round loop's finally has to call it the moment streaming ends.
  const { drainPendingLiveEvents } = useEventStream({
    messages,
    activeChatIdRef,
    streamingRef,
    voiceActiveRef,
    refreshState,
    onGuestJob: setGuestJobs,
    onMessages: setMessages,
    onUnread: setUnreadChats,
    onVoiceAttach: voiceAttachRound,
    onError: setBanner,
    onRoomEvent: (chatId) => refreshRoom(chatId),
  })
  drainRef.current = drainPendingLiveEvents

  // Refetch the room snapshot (roster + open flags) and the remembered
  // voices. Best-effort with the same cross-chat write-guard as every other
  // async fetch: a late response must not paint another chat's roster.
  function refreshRoom(chatId) {
    if (!chatId) return
    api.roster(chatId).then((r) => {
      if (chatId === activeChatIdRef.current) {
        setRoomInfo(r)
        // A spoken introduction or command flips room mode server-side;
        // adopt it on the client too (#28 phases 4 and room commands) so
        // the session's capture profile and the toggle both reflect what
        // is actually running - the parallel pass fires on the server flag
        // regardless of this. The adopt rule lives in roomState.js: an arm
        // always adopts; a disarm ("solo mode" spoken mid-session) adopts
        // only a server-sourced flag, never the user's own session-only
        // toggle.
        const verdict = adoptRoomMode(!!r.room_mode, {
          active: !!voiceRef.current?.active,
          roomMode: !!voiceRef.current?.roomMode,
          source: roomModeSourceRef.current,
        })
        if (verdict !== null) {
          roomModeSourceRef.current = 'server'
          changeRoomMode(verdict)
        }
      }
    }).catch(() => {})
    api.voicePeople().then((d) => setVoicePeople(d.people || [])).catch(() => {})
    refreshVoiceHealth(chatId)
  }

  // Refetch the health strip's snapshot, lightly throttled - room events and
  // label writes can arrive in bursts, and one fetch a second is plenty for
  // a glanceable readout. Same cross-chat write-guard as every async fetch.
  function refreshVoiceHealth(chatId, force = false) {
    if (!chatId) return
    const now = Date.now()
    if (!force && now - healthFetchedAt.current < 1000) return
    healthFetchedAt.current = now
    api.voiceHealth(chatId).then((h) => {
      if (chatId === activeChatIdRef.current) setVoiceHealth(h)
    }).catch(() => {})
  }

  // Tap-to-correct on a labelled turn: reassign, then let the row's
  // message_update event re-render it; refresh the room snapshot for the
  // resolved flags and any roster change.
  async function reassignSpeaker(messageId, name) {
    const chatId = activeChatIdRef.current
    if (!chatId) return
    try {
      await api.reassignSpeaker(chatId, messageId, name)
      const d = await api.messagesAfter(chatId, messageId - 1)
      if (chatId === activeChatIdRef.current && d.messages.length) {
        setMessages((m) => mergeMessagesById(m, d.messages))
      }
      refreshRoom(chatId)
    } catch (e) {
      setBanner(`Could not reassign the turn: ${e.message}`)
    }
  }

  async function dismissRoomFlag(flagId) {
    const chatId = activeChatIdRef.current
    if (!chatId) return
    try {
      await api.resolveRoomFlag(chatId, flagId)
      refreshRoom(chatId)
    } catch { /* the next live event re-syncs */ }
  }

  // Durable room-mode OFF (the "switch off for this chat" action in the
  // voice settings, since the tray's room button became an indicator - #28):
  // the server flag AND this session's client flag both drop.
  async function roomModeOff() {
    if (!activeChat) return
    try {
      mergeChat(await api.updateChat(activeChat.id, { room_mode: false }))
      roomModeSourceRef.current = 'server' // flag now matches durable state
      changeRoomMode(false)
      refreshRoom(activeChat.id)
    } catch (e) {
      setBanner(`Could not switch room mode off: ${e.message}`)
    }
  }

  // First run: no model key works yet → open the guided setup automatically
  // (once per load; it stays reachable from the sidebar footer any time).
  useEffect(() => {
    const k = state.config?.keys
    if (k && !k.anthropic && !k.openai && !setupAutoOpened.current) {
      setupAutoOpened.current = true
      setShowSetup(true)
    }
  }, [state.config])

  useEffect(() => {
    localStorage.setItem('voice_ptt', pttMode ? '1' : '0')
    voiceRef.current?.setManualMode(pttMode)
  }, [pttMode])

  useEffect(() => {
    localStorage.setItem('voice_silence_secs', String(silenceSecs))
    voiceRef.current?.setSilenceMs(silenceSecs * 1000)
  }, [silenceSecs])
  useEffect(() => {
    localStorage.setItem('voice_rate', String(voiceRate))
    voiceRef.current?.setPlaybackRate(voiceRate)
  }, [voiceRate])


  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('theme', theme)
  }, [theme])

  useEffect(() => {
    localStorage.setItem('sidebar', sidebarOpen ? '1' : '0')
  }, [sidebarOpen])

  // Mobile drawer: Esc closes it; its backdrop handles outside taps. The
  // header's ⋯ menu dismisses itself the same way, inside ChatHeader - its ref,
  // its open state and its dismissal are one thing and live together.
  useEffect(() => {
    if (!drawerOpen) return
    const onKey = (e) => { if (e.key === 'Escape') setDrawerOpen(false) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [drawerOpen])

  useEffect(() => {
    stateRef.current = { chatId: activeChatId, participants: state.participants }
  }, [activeChatId, state.participants])

  async function startVoice() {
    try {
      const r = await api.voiceAssign() // make sure everyone has a distinct voice
      setState((s) => ({ ...s, participants: r.participants }))
      // models should know they're being heard aloud - and keep replies short
      if (activeChat && !activeChat.voice_mode) {
        const chat = await api.updateChat(activeChat.id, { voice_mode: true })
        setActiveChat(chat)
      }
      if (!voiceRef.current) {
        voiceRef.current = new VoiceController({
          getChatId: () => stateRef.current.chatId,
          getParticipants: () => stateRef.current.participants,
          sendText: (text, turnId) => { setVoicePartial(null); send(text, [], { batch: false, turnId }) },
          // Live preview of the in-progress transcript. setState is
          // identity-stable, so this once-constructed controller can hold it.
          onPartial: setVoicePartial,
          onState: setVoiceState,
          // mic level → CSS var on the orb, bypassing React (fires ~20Hz)
          onLevel: (v) => {
            // update every orb (desktop dock + mobile call screen); the visible
            // one reacts to the real mic level, the hidden one is harmless
            document.querySelectorAll('.voice-orb').forEach((el) =>
              el.style.setProperty('--level', v.toFixed(3)))
          },
          onError: (msg) => setBanner(msg),
          onHeld: (n) => setHeldVoice(n),
          // Barge-in is the deliberate stop: abort server-side (detached
          // rounds ignore mere disconnects), then drop our stream. Safe to
          // capture once here - stopRound reads only live refs ( step 4).
          onInterruptRound: stopRound,
          // #21: the cause rides the banner - "unavailable" alone left
          // nothing to debug with when the relay died mid-session.
          onSttFallback: (cause) => setBanner('Realtime transcription unavailable'
            + (cause && cause !== 'unknown' ? ` (${cause})` : '')
            + ' - using standard transcription this session.'),
          // orb tint follows the AUDIO: whoever's reply is currently playing
          onSpeaker: (slug) => setSpeakingSlug(slug),
        })
      }
      voiceRef.current.setManualMode(pttMode)
      voiceRef.current.setSilenceMs(silenceSecs * 1000)
      voiceRef.current.setPlaybackRate(voiceRate)
      // The client toggle seeds from the CHAT's durable room mode: a chat an
      // introduction flipped on runs the parallel pass server-side whatever
      // this flag says, so starting "off" would only lie about it - and
      // (#28 phase 4) the capture profile must match from the first
      // utterance. A chat without room mode still starts OFF, exactly as
      // before, so a previous session's toggle never silently doubles a
      // fresh chat's spend.
      const chatRoomOn = !!roomInfo?.room_mode
      roomModeSourceRef.current = 'server' // seeded from the durable flag
      setRoomMode(chatRoomOn)
      voiceRef.current.setRoomMode(chatRoomOn)
      setVoiceMuted(false)
      voiceRef.current.setMuted(false)
      await voiceRef.current.start()
      // Seed the health strip (#28) the moment the session is live; the
      // room events and label writes keep it fresh from here.
      refreshVoiceHealth(activeChatIdRef.current, true)
    } catch (e) {
      setBanner(`Voice failed to start: ${e.message}`)
    }
  }

  // #106: owner-discard of a captured voice turn - the row goes, the UI
  // updates, and the response's honesty (already ingested or not) surfaces
  // as a banner only when there is something to know.
  async function discardTurn(messageId) {
    try {
      const r = await api.discardTurn(activeChatIdRef.current, messageId)
      setMessages((m) => m.filter((x) => x.id !== messageId))
      if (r?.ingested) {
        // #111: hand the owner the next step, not a dead end - membro's
        // eraser deep link lands prefilled with a preview.
        const url = eraseLink(r.memory_ref, window.location.hostname)
        setBanner(url
          ? (<span>Discarded from the chat. A copy had already reached memory -{' '}
              <a href={url} target="_blank" rel="noreferrer" className="underline">erase it there</a>
              {' '}(opens Membro, prefilled).</span>)
          : 'Discarded from the chat. A copy had already reached '
            + 'memory - it stays there (memory is append-only).')
      }
    } catch (e) {
      setBanner(`Could not discard: ${e.message}`)
    }
  }

  function toggleMute() {
    // Real mute (#67): the mic track is disabled at the source - the track
    // emits silence, nothing is detected, recorded, or streamed - and the
    // VAD freezes. The models keep talking; the app stops listening.
    setVoiceMuted((m) => {
      voiceRef.current?.setMuted(!m)
      return !m
    })
  }

  function stopVoice() {
    setVoicePartial(null)
    setVoiceMuted(false)
    voiceRef.current?.stop()
  }

  // One handler for both voice surfaces (dock and mobile call screen): keep
  // the UI state and the controller's session flag in step.
  function changeRoomMode(on) {
    setRoomMode(!!on)
    voiceRef.current?.setRoomMode(on)
  }

  // The manual arm/disarm is DURABLE (#28, fifth field test), and since the
  // room button became an indicator it is reached from the voice settings
  // ("switch on now" / "switch off for this chat"), not the tray. The old
  // session-only semantics both bypassed the ambient/matcher path (the relay
  // ran the slow no-roster pass) and left the seats' room-state line reading
  // the durable flag - so the models said "off" right after the owner
  // switched it on. ON acts like the "group mode" command: durable flag,
  // owner rostered, any sacred ambient-off cleared, seats told the truth.
  // OFF is the existing durable override-off. The client flag just mirrors.
  async function manualRoomMode(on) {
    if (!activeChat) return
    if (!on) return roomModeOff()
    try {
      mergeChat(await api.updateChat(activeChat.id, { room_mode: true }))
      roomModeSourceRef.current = 'server' // durable is the truth now
      changeRoomMode(true)
      refreshRoom(activeChat.id)
    } catch (e) {
      setBanner(`Could not switch room mode on: ${e.message}`)
    }
  }

  // Keep the health strip's live pulse fresh (#28): a diarization pass
  // attaching labels re-renders the turn through the message flow, and that
  // same signal means a new identification decision exists. Throttled inside
  // refreshVoiceHealth; only while a voice session is live.
  useEffect(() => {
    if (voiceState !== 'off') refreshVoiceHealth(activeChatIdRef.current)
  }, [messages, voiceState])

  // #134: poll the capture registry while the page is visible, and
  // immediately when this tab's own voice state changes - the banner must
  // reflect an End within a beat, not a poll cycle.
  useEffect(() => {
    let live = true
    const load = () => api.listCaptures()
      .then((d) => { if (live) setCaptures(d.captures || []) })
      .catch(() => {})
    load()
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') load()
    }, 20000)
    return () => { live = false; clearInterval(t) }
  }, [voiceState])

  // Pin-to-bottom only while the user is at the bottom; the moment they scroll
  // up during streaming, stop following and count new arrivals for the pill.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    if (atBottomRef.current) {
      el.scrollTop = el.scrollHeight
    } else if (messages.length > prevLenRef.current) {
      setNewCount((c) => c + (messages.length - prevLenRef.current))
    }
    prevLenRef.current = messages.length
  }, [messages])

  function onScroll() {
    const el = scrollRef.current
    if (!el) return
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    atBottomRef.current = near
    setAtBottom(near)
    if (near) setNewCount(0)
  }

  function jumpToBottom() {
    const el = scrollRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
    atBottomRef.current = true
    setAtBottom(true)
    setNewCount(0)
  }

  function maybeDistill(chatId) {
    if (!chatId || !dirtyChats.current.has(chatId)) return
    dirtyChats.current.delete(chatId)
    // reflection pass: chat title + project memory
    // distill now runs in the background server-side (instant return); refresh now, then
    // again shortly to pick up the async title/memory once the write finishes.
    api.distill(chatId).then(() => { refreshState(); setTimeout(refreshState, 8000) }).catch(() => {})
  }


  // Leave every full-page surface (Models / Connections / Spend / Voices):
  // the chat pane renders only when ALL page flags are down. Every route
  // back to a chat goes through here; the flags live in pageFlags above, so
  // this stays complete by construction.
  function leavePages() {
    for (const [, setOpen] of pageFlags) setOpen(false)
    setModelsIntent(null)
  }

  async function selectChat(id) {
    teardownLiveView()
    leavePages()
    maybeDistill(activeChatId)
    setActiveChatId(id)
    // Opening a chat is what "reading" it means - clear its unread flag
    // (issue 's live-events dirty-mark for chats you weren't looking at).
    setUnreadChats((s) => {
      if (!s.has(id)) return s
      const next = new Set(s); next.delete(id); return next
    })
    atBottomRef.current = true
    setAtBottom(true)
    setNewCount(0)
    // Restore this chat's pending text batch (per-chat, survives navigation).
    restoreBatchFor(id)
    const data = await api.getChat(id)
    // The user may have switched again while this fetch was in flight; a late or
    // out-of-order response must not repaint a chat that's no longer active -
    // that would leave the header title/body describing a different chat_id than
    // the selection/running badge. Same write-guard the live/poll/reattach
    // paths already apply.
    if (id !== activeChatIdRef.current) return
    setActiveChat(data.chat)
    setMessages(data.messages)
    // Seed the guest status chip from the durable snapshot; live changes
    // then arrive over the global events stream. Best-effort - a failure just
    // means the chip catches up on the next status push.
    setGuestJobs([])
    api.guestJobs(id).then((d) => {
      if (id === activeChatIdRef.current) setGuestJobs(d.guest_jobs || [])
    }).catch(() => {})
    // Seed the room-mode snapshot the same way; live room_roster/room_flag
    // events then keep it fresh.
    setRoomInfo(null)
    refreshRoom(id)
  }

  async function newChat(projectId) {
    teardownLiveView()
    leavePages()
    const chat = await api.createChat({ project_id: projectId })
    await refreshState()
    setActiveChatId(chat.id)
    setActiveChat(chat)
    setMessages([])
    setGuestJobs([])
    setRoomInfo(null)
    restoreBatchFor(chat.id)
  }

  async function deleteChat(id) {
    if (id === activeChatId) teardownLiveView()
    await api.deleteChat(id)
    dirtyChats.current.delete(id)
    dropBatchFor(id)
    if (id === activeChatId) {
      setActiveChatId(null)
      setActiveChat(null)
      setMessages([])
    }
    refreshState()
  }

  // Archive = hide from the sidebar for demos/tidiness. Nothing is deleted -
  // the chat, its transcript, and anything memory learned all stay.
  async function archiveChat(id, archived) {
    if (archived && id === activeChatId) teardownLiveView()
    await api.updateChat(id, { archived })
    if (archived && id === activeChatId) {
      setActiveChatId(null)
      setActiveChat(null)
      setMessages([])
    }
    refreshState()
  }

  async function deleteProject(id) {
    await api.deleteProject(id)
    refreshState()
  }

  async function saveProject(values) {
    if (projectModal?.id) await api.updateProject(projectModal.id, values)
    else await api.createProject(values)
    setProjectModal(null)
    refreshState()
  }

  // PATCH responses don't carry the context estimate - keep the one we have
  const mergeChat = (chat) =>
    setActiveChat((prev) => ({ ...chat, context: chat.context ?? prev?.context }))

  async function toggleParticipant(pid) {
    // Allowed while streaming: the round in flight keeps the roster it started
    // with; the change applies from the next turn. (Quick-muting a chatty
    // agent mid-round is the whole point on the voice call screen.)
    if (!activeChat) return
    const current = activeChat.participant_ids || []
    const next = current.includes(pid) ? current.filter((x) => x !== pid) : [...current, pid]
    if (!next.length) {
      setBanner('A chat needs at least one participant.')
      return
    }
    mergeChat(await api.updateChat(activeChat.id, { participant_ids: next }))
  }

  async function toggleVoice() {
    if (!activeChat) return
    mergeChat(await api.updateChat(activeChat.id, { voice_mode: !activeChat.voice_mode }))
  }

  async function toggleWeb() {
    if (!activeChat) return
    mergeChat(await api.updateChat(activeChat.id, { web_enabled: !activeChat.web_enabled }))
  }

  async function toggleMemory() {
    if (!activeChat) return
    mergeChat(await api.updateChat(activeChat.id, { memory_enabled: !activeChat.memory_enabled }))
  }

  async function toggleCode() {
    if (!activeChat) return
    mergeChat(await api.updateChat(activeChat.id, { code_enabled: !activeChat.code_enabled }))
  }


  const cfg = state.config
  const memory = state.memory // { available, url } - companion memory service status
  const keysMissing = cfg && (!cfg.keys.anthropic || !cfg.keys.openai)
  const activeProject = activeChat?.project_id
    ? state.projects.find((p) => p.id === activeChat.project_id)
    : null
  const chatParticipants = activeChat
    ? state.participants.filter(
        (p) => p.enabled && (activeChat.participant_ids || []).includes(p.id),
      )
    : []
  function copyWholeChat() {
    if (!activeChat) return
    const lines = [`# ${activeChat.title}`, '']
    for (const m of messages) {
      const { label } = participantInfo(m.speaker, state.participants)
      lines.push(`**${label}:** ${(m.content || '').trim() || '(no text)'}`, '')
    }
    navigator.clipboard.writeText(lines.join('\n').trim() + '\n')
    setCopiedChat(true)
    setTimeout(() => setCopiedChat(false), 1500)
  }

  const lastMsg = messages[messages.length - 1]
  const canContinue = !streaming && lastMsg && lastMsg.speaker !== 'user'

  // The chat total comes from the server, off the same scan the export
  // picker reads, so the two surfaces cannot print different dollars for one
  // chat. It refreshes when the chat opens and at round end, not per token:
  // voice and utility spend live in tables the browser never sees.
  const chatTotal = activeChat?.cost
  // The bar above the composer; the header ring reads the same gauge.
  const composerGauge = contextGauge(activeChat?.context)

  // Room mode (#28 phase 2) derivations - all decision logic in roomState.js.
  const rosterText = rosterChipText(roomInfo?.roster)
  const rosterHint = rosterTitle(roomInfo?.roster, roomInfo?.sufficient_seconds,
                                 roomInfo?.min_short_clips)
  // The ask strip serves both question kinds: "someone new - who?" and the
  // merge question ("is X the same person as Y?"). One at a time each,
  // unknown-voice first (it is about the CURRENT speaker).
  const openAsk = askFlag(roomInfo?.flags) || mergeFlag(roomInfo?.flags)
  // The voice health strip (#28), derived in voiceHealth.js (pure): only
  // while a session is live - it describes the machinery under a live call.
  const health = voiceState !== 'off'
    ? healthStrip({
        health: voiceHealth,
        people: voicePeople,
        // The roster feeds the per-person chips (#28, the dock refinement):
        // in a room the chips describe who is IN it; with no roster they
        // fall back to the remembered voices. Rule in roomState.js.
        roster: roomInfo?.roster,
        sufficientSeconds: roomInfo?.sufficient_seconds,
        minShortClips: roomInfo?.min_short_clips,
        sessionActive: true,
      })
    : null
  const mismatchFlags = mismatchByMessage(roomInfo?.flags)

  // Running-task badge: whether this chat has a round/agent working. It
  // keeps going if you switch away, so the badge is per-chat, not per-view.
  const activeRunning = isChatRunning(runningChats, activeChatId)
  // Header title must describe the same chat_id as the running badge and the
  // sidebar selection, never a stale `activeChat` still loading behind a
  // switch. Sourced strictly by activeChatId, falling back to the sidebar copy.
  const activeTitle = resolveHeaderTitle(activeChat, activeChatId, state.chats)


  // Sidebar element, reused by the desktop static column and the mobile drawer.
  // On mobile, selecting a chat closes the drawer.
  const sidebarEl = (closeAfter) => (
    <Sidebar
      onCollapse={() => (closeAfter ? setDrawerOpen(false) : setSidebarOpen(false))}
      projects={state.projects}
      chats={state.chats}
      activeChatId={activeChatId}
      runningChats={runningChats}
      unreadChats={unreadChats}
      onSelectChat={(id) => { selectChat(id); if (closeAfter) setDrawerOpen(false) }}
      onNewChat={(pid) => { newChat(pid); if (closeAfter) setDrawerOpen(false) }}
      onNewProject={() => setProjectModal({})}
      onDeleteChat={deleteChat}
      onArchiveChat={archiveChat}
      onDeleteProject={deleteProject}
      onEditProject={(p) => setProjectModal(p)}
      onMoveChat={async (chatId, projectId) => {
        const updated = await api.updateChat(chatId, { project_id: projectId })
        if (chatId === activeChatId) mergeChat(updated)
        refreshState()
      }}
      onManageModels={() => { leavePages(); setShowParticipants(true); if (closeAfter) setDrawerOpen(false) }}
      onOpenIntegrations={() => { leavePages(); setShowIntegrations(true); if (closeAfter) setDrawerOpen(false) }}
      onOpenImport={() => setShowImport(true)}
      onOpenExport={() => setShowExport(true)}
      onOpenCost={() => { leavePages(); setShowCost(true); if (closeAfter) setDrawerOpen(false) }}
      onOpenVoices={() => { leavePages(); setShowVoices(true); if (closeAfter) setDrawerOpen(false) }}
      theme={theme}
      onToggleTheme={setTheme}
    />
  )

  return (
    <div className="h-full flex">
      {/* Desktop (sm+): static sidebar column, or a thin rail when collapsed.
          Hidden on mobile, where the sidebar lives in the drawer below. */}
      <div className="hidden sm:contents">
        {sidebarOpen ? sidebarEl(false) : (
          <div className="w-11 shrink-0 border-r border-edge bg-app flex flex-col items-center py-3 gap-2">
            <button title="Show sidebar" aria-label="Show sidebar" onClick={() => setSidebarOpen(true)} className="text-ink-dim hover:text-ink p-1.5"><PanelLeft size={18} /></button>
            <button title="New chat" aria-label="New chat" onClick={() => newChat(null)} className="text-ink-dim hover:text-ink p-1.5"><Plus size={18} /></button>
          </div>
        )}
      </div>
      {/* Mobile: off-canvas drawer + dimmed backdrop. */}
      {drawerOpen && (
        <div className="sm:hidden">
          <div className="drawer-backdrop" onClick={() => setDrawerOpen(false)} />
          <div className="drawer-panel flex">{sidebarEl(true)}</div>
        </div>
      )}
      <main className="flex-1 flex flex-col min-w-0">
        {banner && (
          <div className="bg-red-950/70 border-b border-red-900 text-red-200 text-sm px-4 py-2 flex">
            <span className="flex-1 inline-flex items-center gap-1.5"><AlertTriangle size={14} /> {banner}</span>
            <button aria-label="Dismiss" onClick={() => setBanner(null)}><X size={14} /></button>
          </div>
        )}
        {keysMissing && (
          <div className="bg-amber-950/70 border-b border-amber-900 text-amber-200 text-sm px-4 py-2">
            ⚠ Missing API key{!cfg.keys.anthropic && !cfg.keys.openai ? 's' : ''}:
            {!cfg.keys.anthropic && ' ANTHROPIC_API_KEY'}
            {!cfg.keys.anthropic && !cfg.keys.openai && ' and'}
            {!cfg.keys.openai && ' OPENAI_API_KEY'}
            {' '}-{' '}
            <button className="underline hover:text-amber-100" onClick={() => setShowSetup(true)}>
              set it up here
            </button>
            {' '}(no restart needed).
          </div>
        )}
        {showIntegrations ? (
          <IntegrationsConsole
            participants={state.participants}
            onClose={() => setShowIntegrations(false)}
            onOpenMenu={() => setDrawerOpen(true)}
            onManageParticipants={(intent) => {
              setModelsIntent(intent ? { ...intent, from: 'connections' } : null)
              setShowIntegrations(false)
              setShowParticipants(true)
            }}
            onChanged={refreshState}
          />
        ) : showCost ? (
          <SpendPage
            onClose={() => setShowCost(false)}
            onOpenMenu={() => setDrawerOpen(true)}
          />
        ) : showVoices ? (
          <VoicesPage
            onClose={() => setShowVoices(false)}
            onOpenMenu={() => setDrawerOpen(true)}
          />
        ) : showParticipants ? (
          <ModelsPage
            participants={state.participants}
            settings={state.settings}
            voiceEnabled={cfg?.voice_enabled}
            onOpenVoices={() => { leavePages(); setShowVoices(true) }}
            onChanged={refreshState}
            intent={modelsIntent}
            backLabel={modelsIntent?.from === 'connections' ? 'Back to Connections' : 'Back to chat'}
            onClose={() => {
              // Return to where you came from: the console sent you
              // here to add/edit a seat - closing goes back to it, not to
              // the chat you weren't looking at.
              const toConnections = modelsIntent?.from === 'connections'
              setModelsIntent(null)
              setShowParticipants(false)
              if (toConnections) setShowIntegrations(true)
            }}
            onOpenMenu={() => setDrawerOpen(true)}
          />
        ) : activeChat ? (
          <>
            <ChatHeader
              activeChat={activeChat}
              activeTitle={activeTitle}
              activeProject={activeProject}
              participants={state.participants}
              cfg={cfg}
              memory={memory}
              running={activeRunning}
              chatTotal={chatTotal}
              copiedChat={copiedChat}
              voiceState={voiceState}
              onOpenDrawer={() => setDrawerOpen(true)}
              onCopyChat={copyWholeChat}
              onStartVoice={startVoice}
              onEndVoice={stopVoice}
              onToggleParticipant={toggleParticipant}
              onToggleVoice={toggleVoice}
              onToggleWeb={toggleWeb}
              onToggleMemory={toggleMemory}
              onToggleCode={toggleCode}
            />
            <ThreadView
              messages={messages}
              participants={state.participants}
              chatParticipants={chatParticipants}
              mismatchFlags={mismatchFlags}
              roomRoster={roomInfo?.roster || []}
              voicePeople={voicePeople}
              onReassign={reassignSpeaker}
              onDiscard={discardTurn}
              examplePrompts={EXAMPLE_PROMPTS}
              streaming={streaming}
              roundProgress={roundProgress}
              canContinue={canContinue}
              contRounds={contRounds}
              atBottom={atBottom}
              newCount={newCount}
              scrollRef={scrollRef}
              onScroll={onScroll}
              onJumpToBottom={jumpToBottom}
              onContinue={() => continueRound(contRounds)}
              onContRoundsChange={setContRounds}
              onPickPrompt={(t) => setDraft({ text: t })}
            >
              <VoiceDock
                voiceState={voiceState}
                pttMode={pttMode}
                silenceSecs={silenceSecs}
                voiceRate={voiceRate}
                dockOpen={voiceDockOpen}
                roomMode={roomMode}
                rosterText={rosterText}
                rosterHint={rosterHint}
                health={health}
                onRoomModeOff={roomModeOff}
                onPttModeChange={setPttMode}
                onSilenceSecsChange={setSilenceSecs}
                onVoiceRateChange={setVoiceRate}
                onDockOpenChange={setVoiceDockOpen}
                onRoomModeChange={manualRoomMode}
                onFinalizeNow={() => voiceRef.current?.finalizeNow()}
                onInterrupt={() => voiceRef.current?.interrupt()}
                onStop={stopVoice}
                muted={voiceMuted}
                onToggleMute={toggleMute}
              />
            </ThreadView>
            {/* Global context indicator: thin bar atop the composer. Same
                gauge as the header ring (headerView.contextGauge) - the two used
                to compute the same percentage and then disagree about when it
                turned red, at 90% here and 85% there. */}
            {composerGauge && (
              <div
                className="h-0.5 w-full shrink-0"
                style={{ background: 'var(--t-edge2)' }}
                title={`Context every reply re-reads: ~${Math.round(composerGauge.pct)}% of a sensible budget`}
              >
                <div
                  className="h-full"
                  style={{ width: `${composerGauge.pct}%`, background: composerGauge.cssTone, transition: 'width var(--dur-slow) var(--ease-out), background var(--dur-slow) var(--ease-out)' }}
                />
              </div>
            )}
            {/* Transient status strips (variant A): the Claude Code
                guest and the queued batch sit BETWEEN thread and composer with
                real air - aligned to the thread column, never touching the
                composer. A floating version was tried first and rejected live:
                it sat on top of the "Let them continue" controls. */}
            {(hasVisibleJob(guestJobs, Date.now() / 1000) || pendingCount(activeBatch) > 0
              || openAsk) && (
              <div className="shrink-0 px-3 sm:px-4 py-2 space-y-2">
                <GuestStatusChip jobs={guestJobs} />
                {/* The ask-fallback (#28 phase 2): a voice matched nobody in
                    the room. Answerable in chat - saying or typing the name IS
                    the answer - so this strip only explains and can dismiss. */}
                {openAsk && (
                  <div className="mx-auto w-full max-w-[768px] flex items-center gap-2 text-xs bg-panel2 border border-edge2 rounded-lg px-3 py-1.5">
                    <span className="inline-flex h-1.5 w-1.5 rounded-full bg-sky-400 animate-pulse shrink-0" aria-hidden="true" />
                    <span className="text-ink-mid flex-1 min-w-0">{flagCopy(openAsk)}</span>
                    <button
                      className="inline-flex items-center gap-1 text-ink-dim hover:text-ink shrink-0"
                      title="Dismiss - the voice stays unnamed"
                      aria-label="Dismiss the unknown-voice question"
                      onClick={() => dismissRoomFlag(openAsk.id)}
                    >
                      <X size={12} /> Dismiss
                    </button>
                  </div>
                )}
                {pendingCount(activeBatch) > 0 && (
                  <div className="mx-auto w-full max-w-[768px] flex items-center gap-2 text-xs bg-panel2 border border-edge2 rounded-lg px-3 py-1.5">
                    <span className="inline-flex h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse shrink-0" aria-hidden="true" />
                    <span className="text-ink-mid flex-1 min-w-0">
                      {pendingCount(activeBatch) === 1
                        ? '1 message queued - it sends as one turn when this round finishes.'
                        : `${pendingCount(activeBatch)} messages queued - they combine into one turn when this round finishes.`}
                    </span>
                    <button
                      className="inline-flex items-center gap-1 text-ink-dim hover:text-err shrink-0"
                      title="Cancel the queued messages - nothing has been sent yet"
                      aria-label="Cancel queued messages"
                      onClick={cancelActiveBatch}
                    >
                      <X size={12} /> Cancel
                    </button>
                  </div>
                )}
              </div>
            )}
            <Composer
              onSend={send}
              disabled={!chatParticipants.length}
              streaming={streaming}
              queuedCount={pendingCount(activeBatch)}
              onStop={stopRound}
              chatParticipants={chatParticipants}
              draft={draft}
              slashCommands={cfg?.slash_commands || []}
            />
          </>
        ) : (
          <div className="relative flex-1 flex items-center justify-center text-ink-faint">
            {/* Mobile: the sidebar lives in a drawer, and with no chat open there's
                no header to reach it from - so surface the menu here too. */}
            <button
              className="sm:hidden absolute top-3 left-3 p-2 text-ink-mid hover:text-ink"
              aria-label="Open chats & settings"
              onClick={() => setDrawerOpen(true)}
            >
              <PanelLeft size={20} />
            </button>
            <div className="text-center space-y-3">
              <div className="text-4xl">🗣️</div>
              <p className="text-lg text-ink-mid">cross<span className="text-accent">band</span></p>
              <p className="text-sm">You and your AI roster - one conversation.</p>
              <button
                className="bg-btn text-btn-ink rounded-lg px-5 py-2 text-sm font-semibold hover:bg-btn-hover"
                onClick={() => newChat(null)}
              >
                Start a chat
              </button>
            </div>
          </div>
        )}
      </main>
      {/* #134: a microphone live in ANOTHER window is shown here - on every
          surface - with the kill that #108 could only offer in-tab. */}
      {(() => {
        const mic = captureBanner(captures, voiceRef.current?.captureSid,
                                  activeChatId)
        if (!mic) return null
        return (
          <div className={`fixed top-3 right-3 z-50 flex items-center gap-2 rounded-lg border px-3 py-2 shadow-lg text-xs ${
            mic.level === 'double' ? 'border-red-500/60 bg-red-950/90 text-red-200'
                                   : 'border-amber-400/60 bg-panel text-amber-300'
          }`} role="alert">
            <span>{mic.text}</span>
            {mic.sessions.map((s) => (
              <button key={s.sid}
                className="border border-current rounded px-1.5 py-0.5 hover:opacity-80"
                onClick={() => api.killCapture(s.sid)
                  .then(() => api.listCaptures())
                  .then((d) => setCaptures(d.captures || []))
                  .catch(() => {})}
              >
                End it
              </button>
            ))}
          </div>
        )
      })()}
      {/* #69: while a full page (models menu, connections, spend) is open,
          voice KEEPS RUNNING and the call surfaces give way to the compact
          strip - visible at every width, because the desktop dock unmounts
          with the chat view. */}
      {voiceSurface({ voiceState, pageOpen }) === 'strip' && (
        <VoiceStrip
          voiceState={voiceState}
          muted={voiceMuted}
          onToggleMute={toggleMute}
          onEnd={stopVoice}
          onBack={leavePages}
        />
      )}
      {/* Mobile-only full-screen voice "call" overlay (sm:hidden inside the
          component). Desktop keeps the in-thread .voice-dock above, untouched. */}
      {voiceSurface({ voiceState, pageOpen }) === 'call' && (
        <MobileVoiceCall
          banner={banner}
          onDismissBanner={() => setBanner(null)}
          voiceState={voiceState}
          held={heldSends + heldVoice}
          participants={chatParticipants}
          roster={state.participants.filter((p) => p.enabled)}
          activeIds={activeChat?.participant_ids || []}
          onToggleParticipant={toggleParticipant}
          captions={captions}
          captionHistory={captionHistory}
          speakingSlug={speakingSlug}
          partial={voicePartial}
          onEnd={stopVoice}
          muted={voiceMuted}
          onToggleMute={toggleMute}
          voice={voiceRef.current}
          roomMode={roomMode}
          onRoomModeChange={manualRoomMode}
          onRoomModeOff={roomModeOff}
          rosterText={rosterText}
          rosterHint={rosterHint}
          askText={openAsk ? flagCopy(openAsk) : null}
          health={health}
        />
      )}
      {projectModal !== null && (
        <ProjectModal
          project={projectModal.id ? projectModal : null}
          onSave={saveProject}
          onClose={() => setProjectModal(null)}
        />
      )}
      {showImport && <ImportModal onClose={() => setShowImport(false)} />}
      {showSetup && (
        <SetupWizard
          onClose={() => { setShowSetup(false); refreshState() }}
          onOpenParticipants={() => { setShowSetup(false); setShowParticipants(true) }}
        />
      )}
      {showExport && <ExportModal chats={state.chats} onClose={() => setShowExport(false)} />}
    </div>
  )
}
