// OpenAI-compatible endpoint presets for the "OpenAI / OpenAI-compatible" API style.
//
// The backend already speaks to *any* OpenAI-compatible endpoint: provider
// "openai" + an optional base_url covers hosted providers (Groq, Together, …)
// and local servers (Ollama, LM Studio) alike. These presets just fill the
// base_url + api_key_env fields for you so you don't have to look them up.
//
// Fields per preset:
//   id            stable key for the <select>
//   label         what the user sees
//   base_url      OpenAI-compatible endpoint ('' = SDK default, i.e. real OpenAI)
//   api_key_env   NAME of the env var holding the key (the key itself lives in .env)
//   needsKey      false for keyless local servers (Ollama/LM Studio)
//   keyEnvExample suggested env-var name shown as a hint
//   getKeyUrl     where to create a key (hosted providers only)
//   hint          one plain-English line about this provider
//
// Note: OpenAI's open-weight models (the gpt-oss family) are served *through*
// these - run them locally via Ollama/LM Studio, or hosted via Groq / Together /
// Fireworks / OpenRouter - so there's no separate "gpt-oss" entry. Pick the host
// and set the model id to the gpt-oss variant it serves.
//
// base_urls verified current 2026-07-07.

export const PRESETS = [
  {
    id: 'ollama',
    label: 'Ollama (local)',
    base_url: 'http://localhost:11434/v1',
    api_key_env: '',
    needsKey: false,
    keyEnvExample: '',
    getKeyUrl: '',
    hint: 'Runs models on your own machine - private and free, no key. Install Ollama, `ollama pull llama3.1`, then set the model to that name.',
  },
  {
    id: 'lmstudio',
    label: 'LM Studio (local)',
    base_url: 'http://localhost:1234/v1',
    api_key_env: '',
    needsKey: false,
    keyEnvExample: '',
    getKeyUrl: '',
    hint: 'Runs models on your own machine via the LM Studio app - no key. Start its local server, then set the model to a loaded model id.',
  },
  {
    id: 'openai',
    label: 'OpenAI (default)',
    base_url: '',
    api_key_env: 'OPENAI_API_KEY',
    needsKey: true,
    keyEnvExample: 'OPENAI_API_KEY',
    getKeyUrl: 'https://platform.openai.com/api-keys',
    hint: 'The default OpenAI API. Leave the base URL blank - the SDK points at OpenAI itself.',
  },
  {
    id: 'groq',
    label: 'Groq',
    base_url: 'https://api.groq.com/openai/v1',
    api_key_env: 'GROQ_API_KEY',
    needsKey: true,
    keyEnvExample: 'GROQ_API_KEY',
    getKeyUrl: 'https://console.groq.com/keys',
    hint: 'Very fast hosted inference for open models (Llama, gpt-oss, and more).',
  },
  {
    id: 'together',
    label: 'Together AI',
    base_url: 'https://api.together.xyz/v1',
    api_key_env: 'TOGETHER_API_KEY',
    needsKey: true,
    keyEnvExample: 'TOGETHER_API_KEY',
    getKeyUrl: 'https://api.together.ai/settings/api-keys',
    hint: 'Hosted access to 200+ open models. Model ids are namespaced, e.g. meta-llama/Llama-3.1-8B-Instruct-Turbo.',
  },
  {
    id: 'openrouter',
    label: 'OpenRouter',
    base_url: 'https://openrouter.ai/api/v1',
    api_key_env: 'OPENROUTER_API_KEY',
    needsKey: true,
    keyEnvExample: 'OPENROUTER_API_KEY',
    getKeyUrl: 'https://openrouter.ai/keys',
    hint: 'One key, many providers. Model ids look like openai/gpt-oss-120b or meta-llama/llama-3.1-70b-instruct.',
  },
  {
    id: 'fireworks',
    label: 'Fireworks',
    base_url: 'https://api.fireworks.ai/inference/v1',
    api_key_env: 'FIREWORKS_API_KEY',
    needsKey: true,
    keyEnvExample: 'FIREWORKS_API_KEY',
    getKeyUrl: 'https://fireworks.ai/account/api-keys',
    hint: 'Fast hosted open models. Model ids look like accounts/fireworks/models/llama-v3p1-8b-instruct.',
  },
  {
    id: 'custom',
    label: 'Custom (fill it in yourself)',
    base_url: '',
    api_key_env: '',
    needsKey: true,
    keyEnvExample: '',
    getKeyUrl: '',
    hint: 'Any other OpenAI-compatible endpoint. Enter the base URL and, if it needs one, the env-var name for its key below.',
  },
]

// Match saved base_url/api_key_env back to a preset id (so the editor reopens on
// the right selection). Falls back to 'custom' when nothing matches, or 'openai'
// for the blank-base_url default.
export function matchPreset({ base_url, api_key_env }) {
  const bu = (base_url || '').trim().replace(/\/+$/, '')
  const key = (api_key_env || '').trim()
  if (!bu) {
    // No base_url: the real OpenAI endpoint. Only call it the OpenAI preset when
    // the key env matches (or is blank/default); anything else is Custom.
    if (!key || key === 'OPENAI_API_KEY') return 'openai'
    return 'custom'
  }
  for (const p of PRESETS) {
    if (p.base_url && p.base_url.replace(/\/+$/, '') === bu) return p.id
  }
  return 'custom'
}
