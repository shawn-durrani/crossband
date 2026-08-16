// Thinking control for OpenAI-compatible endpoints (#159).
//
// Qwen3-family models and their peers emit a hidden reasoning trace before the
// first visible token. Nothing in the app could turn that off, so a local seat
// paid for it on every turn, including in voice where it is heard as silence.
// `reasoning_effort` does not reach it: that setting speaks Anthropic's
// output_config.effort and OpenAI's reasoning.effort, and a local server
// implements neither.
//
// There is no agreed field across servers, so the seat NAMES the mechanism its
// own server documents. Mirrors backend/providers.py THINKING_CONTROL_CHOICES
// and _THINKING_EXTRA_BODY; keep the two in sync.
//
// The last option is a prompt hack rather than a request field, and is labelled
// as one. It is offered because some builds honour nothing else, and it is never
// applied unless the owner picks it.

export const THINKING_CONTROLS = [
  {
    value: '',
    label: 'Default (send nothing)',
    hint: 'The model decides. Thinking models will keep emitting their reasoning trace first.',
  },
  {
    value: 'chat_template_kwargs',
    label: 'Off: chat_template_kwargs (vLLM, SGLang, mlx_lm)',
    hint: 'Sends chat_template_kwargs.enable_thinking = false. The usual choice for template-driven local servers.',
  },
  {
    value: 'enable_thinking',
    label: 'Off: top-level enable_thinking (Qwen-style API)',
    hint: 'Sends enable_thinking = false at the top level of the request.',
  },
  {
    value: 'ollama_think',
    label: 'Off: think = false (Ollama)',
    hint: "Sends think = false, which is Ollama's own spelling on its OpenAI-compatible route.",
  },
  {
    value: 'no_think_hint',
    label: 'Off: /no_think prompt hint (last resort)',
    hint: 'Appends /no_think to the system prompt. A prompt hack, not a request field, so use it only when the server supports no real switch.',
  },
]

const VALUES = THINKING_CONTROLS.map((c) => c.value)

// Which controls this seat may pick. Anthropic seats get Default only: Claude's
// thinking already has a setting (Reasoning effort), and two knobs for one
// behaviour is how they start disagreeing.
export function thinkingOptions(provider) {
  return provider === 'openai' ? THINKING_CONTROLS : THINKING_CONTROLS.slice(0, 1)
}

// Can this seat use a thinking control at all, and what should the field say?
// Gated on base_url, not on the model id: the control only rides the classic
// chat-completions leg, which is exactly the endpoint-with-a-base_url case.
// Sniffing for "qwen" would miss every renamed local build and would fire on
// hosted Qwen seats that reject the field.
export function thinkingSupport(provider, base_url) {
  if (provider !== 'openai') {
    return {
      ok: false,
      note: 'Claude seats control thinking through Reasoning effort above.',
    }
  }
  if (!(base_url || '').trim()) {
    return {
      ok: false,
      note: 'Only OpenAI-compatible endpoints with their own base URL. OpenAI itself has no such field, so this stays off for the default endpoint.',
    }
  }
  return {
    ok: true,
    note: 'For local thinking models (Qwen3 and similar). Pick the mechanism your server documents. If the endpoint rejects it, the turn fails with that message rather than pretending the setting applied.',
  }
}

// What the seat should be saved with, given what the form currently holds. A
// control that no longer applies is cleared here rather than stored and
// silently ignored, so the saved row and the sent request always agree.
export function normalizeThinkingControl(provider, base_url, value) {
  const v = value || ''
  if (!VALUES.includes(v)) return ''
  return thinkingSupport(provider, base_url).ok ? v : ''
}

// One line for the seat list, or '' when there is nothing to say.
export function thinkingSummary(participant) {
  const p = participant || {}
  const v = normalizeThinkingControl(p.provider, p.base_url, p.thinking_control)
  if (!v) return ''
  return v === 'no_think_hint' ? 'thinking off (/no_think hint)' : 'thinking off'
}
