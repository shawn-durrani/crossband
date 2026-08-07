import { useEffect, useState } from 'react'
import { api } from '../api'
import { X, Check, Eye, EyeOff, ExternalLink, Copy, RefreshCw, Loader2 } from 'lucide-react'

// First-run setup: one card per capability, plain English throughout.
// A key is pasted, validated live server-side, and written to .env - the
// person never opens a terminal (beyond ./start.sh) or edits a file.

const CLONE_CMD =
  'git clone https://github.com/shawn-durrani/membro ~/dev/membro && cd ~/dev/membro && ./start.sh'

function GetKeyLink({ href, children }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1 text-xs text-link hover:underline"
    >
      {children || 'Get a key'} <ExternalLink size={11} />
    </a>
  )
}

// Paste field + show/hide + Validate button + result line. `service` is the
// backend service id; `done` = already configured (shows green immediately).
function KeyField({ service, done, placeholder, onValidated, secretPlaceholder }) {
  const [key, setKey] = useState('')
  const [secret, setSecret] = useState('')
  const [show, setShow] = useState(false)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null) // {valid, unlocked?|error?}

  const green = done || result?.valid
  if (green) {
    return (
      <p className="flex items-start gap-1.5 text-sm text-ok">
        <Check size={16} className="mt-0.5 shrink-0" />
        <span>
          {result?.unlocked || 'Already set up - this one works.'}
        </span>
      </p>
    )
  }

  async function validate() {
    setBusy(true)
    setResult(null)
    try {
      const r = await api.setupKey(service, key, secretPlaceholder ? secret : undefined)
      setResult(r)
      if (r.valid) onValidated?.()
    } catch (e) {
      setResult({ valid: false, error: `Something went wrong on this computer (${e.message}) - the key was not saved.` })
    } finally {
      setBusy(false)
    }
  }

  const ready = key.trim() && (!secretPlaceholder || secret.trim())
  return (
    <div className="space-y-2">
      <div className="flex gap-2 flex-wrap">
        <div className="relative flex-1 min-w-[220px]">
          <input
            type={show ? 'text' : 'password'}
            className="w-full bg-app border border-edge2 rounded-lg pl-3 pr-9 py-2 text-sm focus:outline-none focus:border-edge3"
            placeholder={placeholder}
            value={key}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => setKey(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && ready && !busy) validate() }}
          />
          <button
            type="button"
            className="absolute right-2 top-1/2 -translate-y-1/2 text-ink-faint hover:text-ink-mid"
            title={show ? 'Hide the key' : 'Show the key'}
            aria-label={show ? 'Hide the key' : 'Show the key'}
            onClick={() => setShow((v) => !v)}
          >
            {show ? <EyeOff size={15} /> : <Eye size={15} />}
          </button>
        </div>
        {secretPlaceholder && (
          <input
            type={show ? 'text' : 'password'}
            className="flex-1 min-w-[180px] bg-app border border-edge2 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-edge3"
            placeholder={secretPlaceholder}
            value={secret}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => setSecret(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && ready && !busy) validate() }}
          />
        )}
        <button
          className="inline-flex items-center gap-1.5 bg-btn text-btn-ink rounded-lg px-4 py-2 text-sm font-semibold hover:bg-btn-hover disabled:opacity-40"
          disabled={!ready || busy}
          onClick={validate}
        >
          {busy && <Loader2 size={14} className="animate-spin" />}
          {busy ? 'Checking…' : 'Validate'}
        </button>
      </div>
      {result && !result.valid && (
        <p className="text-sm text-err">{result.error}</p>
      )}
    </div>
  )
}

function Card({ title, done, cost, link, children }) {
  return (
    <section
      className={`rounded-[10px] border p-4 space-y-2.5 transition-colors ${
        done ? 'border-ok/40 bg-ok/5' : 'border-edge2 bg-panel'
      }`}
    >
      <div className="flex items-center gap-2">
        <h3 className="font-semibold text-sm">{title}</h3>
        {done && (
          <span className="inline-flex items-center gap-1 text-[11px] font-medium text-ok bg-ok/10 rounded-full px-2 py-0.5">
            <Check size={11} /> ready
          </span>
        )}
        <span className="ml-auto">{link}</span>
      </div>
      {children}
      {cost && !done && <p className="text-xs text-ink-faint">{cost}</p>}
    </section>
  )
}

export default function SetupWizard({ onClose, onOpenParticipants }) {
  const [status, setStatus] = useState(null)
  const [checkingMemory, setCheckingMemory] = useState(false)
  const [copied, setCopied] = useState(false)

  async function refresh() {
    try {
      setStatus(await api.setupStatus())
    } catch {
      /* backend briefly unavailable - cards fall back to "not configured" */
    }
  }
  useEffect(() => { refresh() }, [])

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const svc = (name) => status?.services?.[name] || { configured: false, valid: null }
  const modelKeyIn = svc('anthropic').configured || svc('openai').configured
  const memoryUp = svc('memory_service').configured

  async function checkMemoryAgain() {
    setCheckingMemory(true)
    await refresh()
    setCheckingMemory(false)
  }

  function copyCmd() {
    navigator.clipboard.writeText(CLONE_CMD)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-6"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Set up and add capabilities"
    >
      <div
        className="bg-panel border border-edge2 rounded-2xl w-full max-w-2xl max-h-[88vh] overflow-y-auto"
        style={{ boxShadow: 'var(--shadow-pop)' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 bg-panel border-b border-edge px-6 py-4 flex items-start gap-3 z-10">
          <div className="flex-1">
            <h2 className="text-lg font-semibold">Set up your chat room</h2>
            <p className="text-sm text-ink-mid mt-0.5">
              Paste a key, press Validate, watch it turn green - no files to edit, no restart needed.
            </p>
            <p className={`text-sm mt-1.5 ${modelKeyIn ? 'text-ok' : 'text-ink-dim'}`}>
              {modelKeyIn
                ? '✓ You’re ready to chat. Everything below is optional - add it whenever.'
                : 'You can start chatting once one model key is green.'}
            </p>
          </div>
          <button
            className="text-ink-dim hover:text-ink p-1 shrink-0"
            title="Close - you can come back any time from the sidebar"
            aria-label="Close setup"
            onClick={onClose}
          >
            <X size={18} />
          </button>
        </div>

        <div className="p-6 space-y-4">
          <Card
            title="Anthropic - Claude"
            done={svc('anthropic').configured || svc('anthropic').valid}
            link={<GetKeyLink href="https://console.anthropic.com" />}
            cost="Pay-as-you-go: what you spend depends heavily on how much you use it and which models are seated. The Spend page tracks it as you go."
          >
            <p className="text-sm text-ink-mid">
              Puts Claude in the room. Claude also writes the chat titles and the running
              summaries behind the scenes, so this key does double duty.
            </p>
            <KeyField
              service="anthropic"
              done={svc('anthropic').configured}
              placeholder="Paste your Anthropic key (starts with sk-ant-…)"
              onValidated={refresh}
            />
          </Card>

          <Card
            title="OpenAI - GPT"
            done={svc('openai').configured || svc('openai').valid}
            link={<GetKeyLink href="https://platform.openai.com/api-keys" />}
            cost="Pay-as-you-go: what you spend depends heavily on how much you use it and which models are seated. The Spend page tracks it as you go."
          >
            <p className="text-sm text-ink-mid">
              Puts GPT in the room. With two models present they can compare notes, argue,
              and catch each other&apos;s mistakes - the whole point of this app.
            </p>
            <KeyField
              service="openai"
              done={svc('openai').configured}
              placeholder="Paste your OpenAI key (starts with sk-…)"
              onValidated={refresh}
            />
          </Card>

          <Card
            title="ElevenLabs - voice"
            done={svc('elevenlabs').configured || svc('elevenlabs').valid}
            link={<GetKeyLink href="https://elevenlabs.io" />}
            cost="Free tier covers light use; beyond that it's a monthly plan."
          >
            <p className="text-sm text-ink-mid">
              The key you want: every model speaks in its own voice, and you can talk over
              them to interrupt - a real spoken conversation, hands free.
            </p>
            <KeyField
              service="elevenlabs"
              done={svc('elevenlabs').configured}
              placeholder="Paste your ElevenLabs key"
              onValidated={refresh}
            />
          </Card>

          <Card
            title="Web search - Tavily & Brave"
            done={svc('tavily').configured || svc('brave').configured}
            link={
              <span className="flex items-center gap-3">
                <GetKeyLink href="https://tavily.com">Tavily key</GetKeyLink>
                <GetKeyLink href="https://brave.com/search/api">Brave key</GetKeyLink>
              </span>
            }
            cost="Both have free tiers that cover casual use."
          >
            <p className="text-sm text-ink-mid">
              Lets the models look things up - news, prices, docs - instead of guessing from
              memory. One key is enough; with both, anything found by both engines gets
              flagged as extra trustworthy.
            </p>
            <div className="space-y-2.5">
              <KeyField
                service="tavily"
                done={svc('tavily').configured}
                placeholder="Tavily key (starts with tvly-…)"
                onValidated={refresh}
              />
              <KeyField
                service="brave"
                done={svc('brave').configured}
                placeholder="Brave Search key"
                onValidated={refresh}
              />
            </div>
          </Card>

          <Card
            title="Reddit"
            done={svc('reddit').configured || svc('reddit').valid}
            link={<GetKeyLink href="https://www.reddit.com/prefs/apps">Create a free app</GetKeyLink>}
            cost="Free. We can't test this one without making a real request, so it's saved as-is."
          >
            <p className="text-sm text-ink-mid">
              Lets the models read Reddit threads and comments while researching. Create a
              free &ldquo;script&rdquo; app on Reddit and paste both parts here.
            </p>
            <KeyField
              service="reddit"
              done={svc('reddit').configured}
              placeholder="Client id (under the app name)"
              secretPlaceholder="Secret"
              onValidated={refresh}
            />
          </Card>

          <Card
            title="Open-source & local models"
            link={<GetKeyLink href="https://ollama.com/download">Get Ollama</GetKeyLink>}
          >
            <p className="text-sm text-ink-mid">
              You&apos;re not limited to Claude and GPT. Run open models on your own
              machine - private and free, no key - with{' '}
              <a href="https://ollama.com" target="_blank" rel="noreferrer" className="text-link hover:underline">Ollama</a>{' '}
              or{' '}
              <a href="https://lmstudio.ai" target="_blank" rel="noreferrer" className="text-link hover:underline">LM Studio</a>,
              or reach hosted open models through an OpenAI-compatible provider (Groq,
              Together, OpenRouter, Fireworks). There&apos;s no key field here: add these
              from the participants editor, which has ready-made presets for each.
            </p>
            {onOpenParticipants ? (
              <button
                className="inline-flex items-center gap-1.5 border border-edge2 rounded-lg px-3 py-1.5 text-sm text-ink-mid hover:text-ink hover:border-edge3"
                onClick={onOpenParticipants}
              >
                ⚙ Open AI participants
              </button>
            ) : (
              <p className="text-xs text-ink-faint">
                Open <span className="text-ink-mid">⚙ AI participants</span> from the sidebar,
                add a model, choose the &ldquo;OpenAI / OpenAI-compatible&rdquo; API style, then
                pick a preset. See <span className="text-ink-mid">docs/MODELS.md</span> for the step-by-step.
              </p>
            )}
          </Card>

          <Card
            title="Shared memory"
            done={memoryUp}
            link={<GetKeyLink href="https://github.com/shawn-durrani/membro">About</GetKeyLink>}
          >
            {memoryUp ? (
              <p className="flex items-start gap-1.5 text-sm text-ok">
                <Check size={16} className="mt-0.5 shrink-0" />
                <span>Memory service detected - every chat starts with your cheat-sheet, and you can review every fact it keeps.</span>
              </p>
            ) : (
              <>
                <p className="text-sm text-ink-mid">
                  A small companion app that keeps a ledger of facts about you - like a
                  cheat-sheet the models read before every conversation, where every card
                  is yours to review, correct, or erase. No key needed; it just has to be
                  running. Paste this into the Terminal app to install and start it:
                </p>
                <div className="relative">
                  <pre className="bg-app border border-edge rounded-lg p-3 pr-10 text-xs overflow-x-auto whitespace-pre-wrap break-all font-mono text-ink-mid">{CLONE_CMD}</pre>
                  <button
                    className="absolute right-2 top-2 text-ink-dim hover:text-ink p-1"
                    title="Copy the command"
                    aria-label="Copy the install command"
                    onClick={copyCmd}
                  >
                    {copied ? <Check size={14} className="text-ok" /> : <Copy size={14} />}
                  </button>
                </div>
                <button
                  className="inline-flex items-center gap-1.5 border border-edge2 rounded-lg px-3 py-1.5 text-sm text-ink-mid hover:text-ink hover:border-edge3 disabled:opacity-40"
                  disabled={checkingMemory}
                  onClick={checkMemoryAgain}
                >
                  <RefreshCw size={13} className={checkingMemory ? 'animate-spin' : ''} />
                  {checkingMemory ? 'Checking…' : 'Check again'}
                </button>
              </>
            )}
          </Card>
        </div>

        <div className="sticky bottom-0 bg-panel border-t border-edge px-6 py-3 flex justify-end">
          <button
            className="text-sm text-ink-mid hover:text-ink border border-edge2 hover:border-edge3 rounded-lg px-4 py-2"
            onClick={onClose}
          >
            {modelKeyIn ? 'Done' : 'Do this later'}
          </button>
        </div>
      </div>
    </div>
  )
}
