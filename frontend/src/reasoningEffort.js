// Reasoning-effort rules for the seat editor (#236). Mirrors
// backend/providers.py: REASONING_CHOICES for the vocabulary,
// _ANTHROPIC_NO_EFFORT_MODEL and the gpt-5/o-series gate for model
// support - keep the two in sync.

// Does this model support a reasoning-effort setting? Mirrors the per-provider
// gating in backend/providers.py (REASONING_CHOICES / _anthropic_unsupported_model)
// so the UI can grey it out when it won't apply (the runtime also drops it
// gracefully as a backstop). The policy is authoritative: Default sends no
// override at all, a fixed level sends only that level, and Adaptive
// (Anthropic-only) is the SOLE way to get provider-decided variable-duration
// thinking. Voice mode never silently downgrades any of this.
export function effortSupport(provider, model) {
  const m = (model || '').toLowerCase()
  if (provider === 'anthropic') {
    if (/haiku|claude-3|sonnet-4-5|sonnet-4\.5|sonnet-4-0/.test(m))
      return { ok: false, note: `Not supported on this Claude model (Haiku / Sonnet 4.5) - it would be ignored.` }
    return { ok: true, note: `Default sends no override. Low/Medium/High/Max set output_config.effort ("Max" needs Opus 4.6+, otherwise uses High) - a bounded, non-deliberating request. Adaptive lets Claude decide how long to think per reply, which can add several seconds - including in voice - before the first word.` }
  }
  if (/^gpt-5|^o1|^o3|^o4/.test(m))
    return { ok: true, note: `OpenAI: Default sends no override; Low/Medium/High set reasoning.effort ("Max" maps to High). Applies in tool-free chats; automatically skipped when 🌐 research tools are on (provider limitation). No Adaptive option - that's an Anthropic-only concept.` }
  return { ok: false, note: `Only OpenAI reasoning models (gpt-5.x, o-series) support reasoning effort - it would be ignored otherwise.` }
}

// The <select> options for this provider - Default + the fixed levels always;
// Adaptive appended ONLY for Anthropic (backend/providers.py's
// ANTHROPIC_REASONING_CHOICES vs OPENAI_REASONING_CHOICES - a future
// OpenAI-compatible provider added the same way, provider="openai" plus a
// custom base_url, gets the same OpenAI set here automatically since it's
// keyed on `provider`, not on model or base_url).
export function reasoningOptions(provider) {
  const fixed = [
    { value: '', label: 'Default (no effort/thinking override - provider’s plain default)' },
    { value: 'low', label: 'Low' },
    { value: 'medium', label: 'Medium' },
    { value: 'high', label: 'High' },
    { value: 'max', label: 'Max' },
  ]
  if (provider === 'anthropic') {
    fixed.push({ value: 'adaptive',
                label: 'Adaptive (Claude decides thinking duration - can add seconds, incl. in voice)' })
  }
  return fixed
}

// What the seat should be saved with, given what the form holds.
// Provider-keyed exactly like providers.py's valid_reasoning_effort: a
// value outside this provider's vocabulary ('adaptive' on an OpenAI seat,
// a stale hand-edited level) resets to Default here rather than being
// sent and 400'd on the next unrelated save. Deliberately not model-keyed:
// an effort on a model that ignores it is valid to store (the backend
// degrades it gracefully), and clearing it would wipe a kept setting
// while the owner is mid-typo in the model field.
export function normalizeReasoningEffort(provider, value) {
  const v = value || ''
  return reasoningOptions(provider).some((o) => o.value === v) ? v : ''
}
