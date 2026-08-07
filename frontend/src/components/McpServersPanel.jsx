// Connections → manage MCP servers. Derivations live in mcpServersView.js;
// this component only renders and calls the API.
import { useCallback, useEffect, useState } from 'react'
import {
  AlertTriangle, CheckCircle2, Clock3, FlaskConical, Loader2, Pencil,
  Plus, Server, Trash2, X,
} from 'lucide-react'
import { buildSpec, draftFromRow, rowStatus } from '../mcpServersView.js'

const EMPTY = { name: '', slot: 'models', commandLine: '', envText: '', label: '' }

const TONE = {
  ok: 'text-ok', pending: 'text-warn', error: 'text-err',
}
const DOT = {
  ok: CheckCircle2, pending: Clock3, error: AlertTriangle,
}

export default function McpServersPanel() {
  const [data, setData] = useState(null)
  const [draft, setDraft] = useState(null)   // null = form closed
  const [busy, setBusy] = useState(false)
  const [formErrors, setFormErrors] = useState([])
  const [testResult, setTestResult] = useState(null)
  const [pageError, setPageError] = useState(null)

  const load = useCallback(async () => {
    try {
      const r = await fetch('/api/mcp-servers')
      setData(await r.json())
      setPageError(null)
    } catch {
      setPageError('Could not load MCP servers.')
    }
  }, [])
  useEffect(() => { load() }, [load])

  const save = async () => {
    const spec = buildSpec(draft)
    if (spec.errors) { setFormErrors(spec.errors); return }
    setBusy(true)
    try {
      const r = await fetch(`/api/mcp-servers/${spec.slot}/${spec.name}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(spec.payload),
      })
      if (!r.ok) {
        setFormErrors([(await r.json()).detail || 'save failed'])
        return
      }
      setDraft(null); setFormErrors([]); setTestResult(null)
      await load()
    } finally { setBusy(false) }
  }

  const remove = async (row) => {
    if (!window.confirm(`Remove MCP server "${row.name}"? Its tools disappear at the next restart.`)) return
    await fetch(`/api/mcp-servers/${row.slot}/${row.name}`, { method: 'DELETE' })
    await load()
  }

  // Test the DRAFT if the form is open, else a saved row - same endpoint.
  const test = async (rowOrNull) => {
    const spec = rowOrNull
      ? buildSpec(draftFromRow(rowOrNull))
      : buildSpec(draft)
    if (spec.errors) { setFormErrors(spec.errors); return }
    setBusy(true); setTestResult({ running: true, name: spec.name })
    try {
      const r = await fetch('/api/mcp-servers/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(spec.payload),
      })
      setTestResult({ name: spec.name, ...(await r.json()) })
    } catch {
      setTestResult({ name: spec.name, ok: false, error: 'request failed' })
    } finally { setBusy(false) }
  }

  const field = 'w-full bg-transparent border border-edge2 rounded-lg px-2.5 py-1.5 text-sm text-ink placeholder:text-ink-faint focus:border-edge3 outline-none'
  const rows = data?.servers || []

  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.06em] text-ink-dim">
          Manage MCP servers
        </h2>
        <p className="text-xs text-ink-faint mt-0.5">
          An MCP server is a small local program that gives your models extra tools.
          &ldquo;Chat models&rdquo; servers are used in every conversation; &ldquo;coding guests&rdquo;
          servers are mounted only when you summon Claude Code. Entries are stored in
          config.local.json - private to this machine.
        </p>
      </div>

      {data?.restart_required && (
        <div className="flex items-start gap-2 text-sm border border-warn/40 bg-warn/10 text-warn rounded-lg px-3 py-2">
          <Clock3 size={15} className="mt-0.5 shrink-0" />
          <span>
            Changes are saved but the running service predates them. Restart it to apply:
            <code className="block mt-1 text-xs opacity-90">
              ./start.sh
            </code>
            <span className="block mt-1 text-xs opacity-90">
              If you installed the optional background agent, restart that instead
              (see docs/OPERATIONS.md).
            </span>
          </span>
        </div>
      )}
      {pageError && (
        <div className="flex items-center gap-1.5 text-sm text-err">
          <AlertTriangle size={14} /> {pageError}
        </div>
      )}

      <div className="space-y-2">
        {rows.map((row) => {
          const st = rowStatus(row)
          const Dot = DOT[st.tone]
          return (
            <div key={`${row.slot}/${row.name}`}
              className="border border-edge2 rounded-lg px-3 py-2.5 flex items-start gap-3">
              <Server size={15} className="text-ink-dim mt-0.5 shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-medium">{row.name}</span>
                  <span className="text-[10px] uppercase tracking-wide border border-edge2 rounded px-1.5 py-px text-ink-dim">
                    {row.slot === 'models' ? 'chat models' : 'coding guests'}
                  </span>
                  {row.label && <span className="text-xs text-ink-faint">“{row.label}”</span>}
                </div>
                <div className="text-xs text-ink-faint truncate mt-0.5">
                  {[row.command, ...(row.args || [])].join(' ')}
                </div>
                <div className={`flex items-center gap-1.5 text-xs mt-1 ${TONE[st.tone]}`}>
                  <Dot size={13} className="shrink-0" /> <span className="truncate">{st.text}</span>
                </div>
                {testResult && !testResult.running && testResult.name === row.name && (
                  <div className={`text-xs mt-1 ${testResult.ok ? 'text-ok' : 'text-err'}`}>
                    {testResult.ok
                      ? `test passed - tools: ${testResult.tools.join(', ')}`
                      : `test failed - ${testResult.error}`}
                  </div>
                )}
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <button title="Spawn this server now and list its tools - the difference between configured and working"
                  className="text-ink-dim hover:text-ink p-1.5 disabled:opacity-40"
                  disabled={busy} onClick={() => test(row)}>
                  {testResult?.running && testResult.name === row.name
                    ? <Loader2 size={15} className="animate-spin" /> : <FlaskConical size={15} />}
                </button>
                <button title="Edit" className="text-ink-dim hover:text-ink p-1.5"
                  onClick={() => { setDraft(draftFromRow(row)); setFormErrors([]); setTestResult(null) }}>
                  <Pencil size={15} />
                </button>
                <button title="Remove" className="text-ink-dim hover:text-err p-1.5"
                  onClick={() => remove(row)}>
                  <Trash2 size={15} />
                </button>
              </div>
            </div>
          )
        })}
        {data && !rows.length && (
          <p className="text-xs text-ink-faint">No MCP servers configured yet.</p>
        )}
      </div>

      {draft ? (
        <div className="border border-edge2 rounded-lg p-3 space-y-2.5">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium">{draft._editing ? 'Edit server' : 'Add a server'}</span>
            <button className="text-ink-dim hover:text-ink p-1" title="Close"
              onClick={() => { setDraft(null); setFormErrors([]) }}><X size={15} /></button>
          </div>
          <div className="grid sm:grid-cols-2 gap-2.5">
            <label className="text-xs text-ink-mid space-y-1">
              <span>Name (becomes the mcp__name__tool namespace)</span>
              <input className={field} value={draft.name} placeholder="my-server"
                onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
            </label>
            <label className="text-xs text-ink-mid space-y-1">
              <span>Used by</span>
              <select className={field} value={draft.slot}
                onChange={(e) => setDraft({ ...draft, slot: e.target.value })}>
                <option value="models">Chat models (every conversation)</option>
                <option value="guests">Coding guests (summoned Claude Code)</option>
              </select>
            </label>
          </div>
          <label className="text-xs text-ink-mid space-y-1 block">
            <span>Command (executable + args, split on spaces - no shell quoting)</span>
            <input className={field} value={draft.commandLine}
              placeholder="/Users/you/dev/tool/.venv/bin/python -m tool.mcp_server"
              onChange={(e) => setDraft({ ...draft, commandLine: e.target.value })} />
          </label>
          <div className="grid sm:grid-cols-2 gap-2.5">
            <label className="text-xs text-ink-mid space-y-1">
              <span>Environment (KEY=VALUE per line, optional)</span>
              <textarea className={`${field} h-16 font-mono text-xs`} value={draft.envText}
                placeholder={'PYTHONPATH=/Users/you/dev/tool'}
                onChange={(e) => setDraft({ ...draft, envText: e.target.value })} />
            </label>
            <label className="text-xs text-ink-mid space-y-1">
              <span>Working label (shown while its tools run, optional)</span>
              <input className={field} value={draft.label} placeholder="Looking that up"
                onChange={(e) => setDraft({ ...draft, label: e.target.value })} />
            </label>
          </div>
          {formErrors.length > 0 && (
            <ul className="text-xs text-err space-y-0.5">
              {formErrors.map((e) => <li key={e}>• {e}</li>)}
            </ul>
          )}
          {testResult && !testResult.running && testResult.name === (draft.name || '').trim() && (
            <div className={`text-xs ${testResult.ok ? 'text-ok' : 'text-err'}`}>
              {testResult.ok
                ? `test passed - tools: ${testResult.tools.join(', ')}`
                : `test failed - ${testResult.error}`}
            </div>
          )}
          <div className="flex items-center gap-2">
            <button className="inline-flex items-center gap-1.5 border border-edge2 rounded-lg px-3 py-1.5 text-sm text-ink-mid hover:text-ink hover:border-edge3 disabled:opacity-50"
              disabled={busy} onClick={() => test(null)}>
              {testResult?.running ? <Loader2 size={14} className="animate-spin" /> : <FlaskConical size={14} />}
              Test first
            </button>
            <button className="inline-flex items-center gap-1.5 bg-btn hover:bg-btn-hover border border-edge2 rounded-lg px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-50"
              disabled={busy} onClick={save}>
              Save
            </button>
          </div>
        </div>
      ) : (
        <button className="inline-flex items-center gap-1.5 border border-edge2 rounded-lg px-3 py-1.5 text-sm text-ink-mid hover:text-ink hover:border-edge3"
          onClick={() => { setDraft({ ...EMPTY }); setTestResult(null) }}>
          <Plus size={14} /> Add MCP server
        </button>
      )}
    </section>
  )
}
