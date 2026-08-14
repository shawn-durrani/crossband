// The browser gate (#25): when any API call comes back 401, the session
// died (logout elsewhere, expiry, a restart) - AuthGate registers a handler
// here so the whole app falls back to the lock screen instead of each
// caller surfacing its own cryptic error.
let onUnauthorized = null
export function setUnauthorizedHandler(cb) { onUnauthorized = cb }

async function json(res) {
  if (!res.ok) {
    if (res.status === 401 && onUnauthorized) onUnauthorized()
    let detail = res.statusText
    try { detail = (await res.json()).detail || detail } catch { /* ignore */ }
    throw new Error(detail)
  }
  return res.json()
}

export const api = {
  // The gate's own surface. authSession is deliberately raw fetch->json with
  // no 401 hook: probing the lock state must never recurse into the handler.
  authSession: () => fetch('/api/auth/session').then((r) => r.json()),
  authSetup: (body) => fetch('/api/auth/setup', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).then(json),
  authReset: (body) => fetch('/api/auth/reset', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).then(json),
  authLogin: (password) => fetch('/api/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }),
  }).then(json),
  authLogout: () => fetch('/api/auth/logout', { method: 'POST' }).then(json),
  webauthnLoginOptions: () => fetch('/api/webauthn/login/options', { method: 'POST' }).then(json),
  webauthnLogin: (body) => fetch('/api/webauthn/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).then(json),
  webauthnRegisterOptions: () => fetch('/api/webauthn/register/options', { method: 'POST' }).then(json),
  webauthnRegister: (body) => fetch('/api/webauthn/register', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).then(json),
  webauthnCredentials: () => fetch('/api/webauthn/credentials').then(json),
  webauthnLabel: (id, label) => fetch('/api/webauthn/credentials/label', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, label }),
  }).then(json),
  webauthnRemove: (id) => fetch('/api/webauthn/credentials/remove', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id }),
  }).then(json),
  state: () => fetch('/api/state').then(json),
  createProject: (body) => fetch('/api/projects', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).then(json),
  updateProject: (id, body) => fetch(`/api/projects/${id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).then(json),
  deleteProject: (id) => fetch(`/api/projects/${id}`, { method: 'DELETE' }).then(json),
  createChat: (body) => fetch('/api/chats', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).then(json),
  getChat: (id) => fetch(`/api/chats/${id}`).then(json),
  // Incremental catch-up: only rows newer than `after`, same shape as
  // getChat's `messages`. This is what the global events stream's live
  // wake-up hydrates from, instead of refetching the whole transcript.
  messagesAfter: (id, after) => fetch(`/api/chats/${id}/messages?after=${after}`).then(json),
  // Snapshot of a chat's Claude Code guest jobs: seeds the status chip on
  // open; live changes then arrive over the global events stream.
  guestJobs: (id) => fetch(`/api/chats/${id}/guest_jobs`).then(json),
  updateChat: (id, body) => fetch(`/api/chats/${id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).then(json),
  deleteChat: (id) => fetch(`/api/chats/${id}`, { method: 'DELETE' }).then(json),
  createParticipant: (body) => fetch('/api/participants', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).then(json),
  updateParticipant: (id, body) => fetch(`/api/participants/${id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).then(json),
  deleteParticipant: (id) => fetch(`/api/participants/${id}`, { method: 'DELETE' }).then(json),
  voiceStatus: () => fetch('/api/voice/status').then(json),
  voiceVoices: () => fetch('/api/voice/voices').then(json),
  voiceAssign: () => fetch('/api/voice/assign', { method: 'POST' }).then(json),
  updateSettings: (body) => fetch('/api/settings', {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).then(json),
  listModels: (provider, apiKeyEnv, baseUrl) => {
    const q = new URLSearchParams({ provider })
    if (apiKeyEnv) q.set('api_key_env', apiKeyEnv)
    if (baseUrl) q.set('base_url', baseUrl)
    return fetch(`/api/models?${q}`).then(json)
  },
  // Read-only readout of each participant's actual running model: live
  // participants-row model + the model stamped on their last message.
  modelsStatus: () => fetch('/api/models/status').then(json),
  // Rate cards: the effective price for every known + configured model,
  // each tagged builtin/override/unpriced. Saving writes config.local.json,
  // which load_settings() re-reads every call - so a new rate is live on the
  // next round, no restart.
  rateCards: () => fetch('/api/pricing').then(json),
  saveRateCard: (model, body) => fetch(`/api/pricing/${encodeURIComponent(model)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).then(json),
  deleteRateCard: (model) => fetch(`/api/pricing/${encodeURIComponent(model)}`, {
    method: 'DELETE',
  }).then(json),
  distill: (id) => fetch(`/api/chats/${id}/distill`, { method: 'POST' }).then(json),
  uploadAttachment: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    return fetch('/api/attachments', { method: 'POST', body: fd }).then(json)
  },
  youtubeTranscript: (url) => fetch('/api/youtube-transcript', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }),
  }).then(json),
  abortRound: (id) => fetch(`/api/chats/${id}/round/abort`, { method: 'POST' }).then(json),
  // The chat's currently-generating detached round id, if any. A
  // voice-active client that didn't start the round (a Claude Code hand-back)
  // uses it to attach and voice that round; {round_id: null} when idle.
  activeRound: (id) => fetch(`/api/chats/${id}/active_round`).then(json),
  // Cheap reachability check for the hold-and-retry queues (offline in a car:
  // failure must be fast, not a hung request).
  ping: async () => {
    const ctrl = new AbortController()
    const t = setTimeout(() => ctrl.abort(), 3000)
    try {
      const res = await fetch('/api/state', { signal: ctrl.signal })
      return res.ok
    } catch {
      return false
    } finally {
      clearTimeout(t)
    }
  },
  // Room mode (#28 phase 2): the roster/flags snapshot a room_roster or
  // room_flag live event tells us to refetch (the stream itself is
  // content-free), remembered voices, forget, tap-to-correct, flag dismissal.
  roster: (chatId) => fetch(`/api/chats/${chatId}/roster`).then(json),
  voicePeople: () => fetch('/api/voice/people').then(json),
  renameVoice: (personId, name) => fetch(
    `/api/voice/people/${encodeURIComponent(personId)}/name`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }).then(json),
  forgetVoice: (personId) => fetch(`/api/voice/people/${encodeURIComponent(personId)}`, {
    method: 'DELETE',
  }).then(json),
  // Merge one remembered voice into another (#28: naming is law) - banks
  // fold, the oldest person survives, and the owner's chosen name is law.
  mergeVoice: (personId, intoId, name) => fetch(
    `/api/voice/people/${encodeURIComponent(personId)}/merge`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ into: intoId, name }),
    }).then(json),
  // Clip audition (#68): metadata list, an audio URL for a plain <audio>
  // element (same-origin, the session cookie rides along), delete one clip.
  voiceClips: (personId) => fetch(
    `/api/voice/people/${encodeURIComponent(personId)}/clips`).then(json),
  voiceClipAudioUrl: (personId, fname) =>
    `/api/voice/people/${encodeURIComponent(personId)}/clips/` +
    `${encodeURIComponent(fname)}/audio`,
  deleteVoiceClip: (personId, fname) => fetch(
    `/api/voice/people/${encodeURIComponent(personId)}/clips/` +
    `${encodeURIComponent(fname)}`, { method: 'DELETE' }).then(json),
  // Reassignment tools (#90): a person with no voice yet, a clip refiled
  // under the person it belongs to, another spelling for the same person.
  createVoicePerson: (name) => fetch('/api/voice/people', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  }).then(json),
  moveVoiceClip: (personId, fname, to) => fetch(
    `/api/voice/people/${encodeURIComponent(personId)}/clips/` +
    `${encodeURIComponent(fname)}/move`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ to }),
    }).then(json),
  addVoiceAlias: (personId, name) => fetch(
    `/api/voice/people/${encodeURIComponent(personId)}/alias`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }).then(json),
  // The voice health strip's content-free snapshot (#28): matcher state,
  // counts, mode flags and the last identification's path/latency.
  voiceHealth: (chatId) => fetch(
    `/api/voice/health${chatId ? `?chat_id=${chatId}` : ''}`).then(json),
  reassignSpeaker: (chatId, messageId, name) => fetch(
    `/api/chats/${chatId}/messages/${messageId}/speaker`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }).then(json),
  resolveRoomFlag: (chatId, flagId) => fetch(
    `/api/chats/${chatId}/flags/${flagId}/resolve`, { method: 'POST' }).then(json),
  usageSummary: (window = 'all') => fetch(`/api/usage/summary?window=${window}`).then(json),
  // Unified, read-only integration status that the Integrations console
  // renders. probe=false is a cheap dashboard load (no external calls);
  // probe=true additionally runs each capability's own live check.
  integrations: (probe = false) => fetch(`/api/integrations${probe ? '?probe=true' : ''}`).then(json),
  setupStatus: () => fetch('/api/setup/status').then(json),
  setupKey: (service, key, secret) => fetch('/api/setup/key', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ service, key, secret }),
  }).then(json),
}

// Read a Server-Sent-Events stream and invoke onEvent per event.
// Accepts a JSON-able body or FormData (for uploads); method 'GET' for
// re-attaching to a detached round (no body).
export async function streamSSE(url, body, onEvent, signal, method = 'POST') {
  const isForm = typeof FormData !== 'undefined' && body instanceof FormData
  const res = await fetch(url, {
    method,
    headers: isForm || method === 'GET' ? undefined : { 'Content-Type': 'application/json' },
    body: method === 'GET' ? undefined
      : isForm ? body : (body == null ? undefined : JSON.stringify(body)),
    signal,
  })
  if (!res.ok || !res.body) {
    let detail = res.statusText
    try { detail = (await res.json()).detail || detail } catch { /* ignore */ }
    throw new Error(detail)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      const line = raw.split('\n').find((l) => l.startsWith('data: '))
      if (line) onEvent(JSON.parse(line.slice(6)))
    }
  }
}
