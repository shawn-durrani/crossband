// Ollama keep-alive: how long Ollama holds a seat's model after its last request.
//
// Ollama unloads a model five minutes after its last request, so a quiet chat
// costs a reload before the next first word. A seat talks to Ollama through
// its OpenAI-compatible API, and that API carries no keep_alive field at all
// - the value travels Ollama's native keep-alive call instead. This module
// only decides what the FIELD may hold and which seat may carry it; the
// backend is what sends it (backend/providers.py keep_alive_nudge /
// valid_keep_alive - keep the two in sync).
//
// Valid values: an Ollama duration (a number plus a unit, possibly joined:
// "30m", "1h", "24h", "1h30m") or "-1" to hold the model indefinitely.
// Empty means "send nothing": Ollama's own five-minute unload applies, which
// is exactly the behaviour before this setting existed.

// Ollama time.Duration: integer (optionally decimal) + unit, one or more
// segments. "-1" is Ollama's documented "keep loaded forever" sentinel.
const DURATION = /^\d+(?:\.\d+)?(?:ms|s|m|h|d)(?:\d+(?:\.\d+)?(?:ms|s|m|h|d))*$/
export function validKeepAliveValue(value) {
  const v = (value || '').trim()
  return v === '' || v === '-1' || DURATION.test(v)
}

// Which seats may carry the setting at all. Mirrors the backend gate:
// an openai-provider seat WITH its own base URL (the Ollama case). No base
// URL is OpenAI proper, which has no native route for the nudge; Anthropic
// seats have no local model of their own to keep loaded.
export function keepAliveSupport(provider, base_url) {
  if (provider !== 'openai') {
    return {
      ok: false,
      note: 'Ollama seats only - there is no model of a Claude seat to keep loaded.',
    }
  }
  if (!(base_url || '').trim()) {
    return {
      ok: false,
      note: 'Applies to an Ollama endpoint with its own base URL. OpenAI itself has no such field, so it stays off for the default endpoint.',
    }
  }
  return {
    ok: true,
    note: "Ollama holds the model this long after each of the seat's requests: 30m, 1h, 24h - or -1 to hold it indefinitely. Empty leaves Ollama's own five-minute unload in place.",
  }
}

// What the seat should be saved with, given what the form currently holds.
// A value that no longer applies (or never did) is cleared here rather than
// stored and silently ignored, so the saved row and the sent request always
// agree. An unparseable value is cleared the same way.
export function normalizeKeepAlive(provider, base_url, value) {
  const v = (value || '').trim()
  if (!v) return ''
  if (!validKeepAliveValue(v)) return ''
  if (!keepAliveSupport(provider, base_url).ok) return ''
  return v
}
