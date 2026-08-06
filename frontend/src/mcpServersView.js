// Connections → MCP servers: what the panel derives before rendering.
//
// The form speaks operator (one command line, KEY=VALUE env lines); the API
// speaks spec ({command, args[], env{}}). The translation lives here, in one
// tested place, so the panel component stays render-only — same split as
// headerView/integrationsView.

// "python -m my_tool.mcp_server" -> {command, args[]}. Whitespace split
// only, on purpose: MCP specs are exec arrays, not shell strings, and quoting
// rules would be a lie we can't honour. A path with spaces belongs in a
// wrapper script.
export function parseCommandLine(line) {
  const parts = (line || '').trim().split(/\s+/).filter(Boolean)
  if (!parts.length) return null
  return { command: parts[0], args: parts.slice(1) }
}

// One KEY=VALUE per line; blank lines ignored. Returns null on a malformed
// line so the form can point at it instead of silently dropping it.
export function parseEnvLines(text) {
  const env = {}
  for (const raw of (text || '').split('\n')) {
    const line = raw.trim()
    if (!line) continue
    const eq = line.indexOf('=')
    if (eq < 1) return { error: `not KEY=VALUE: "${line.slice(0, 40)}"` }
    env[line.slice(0, eq).trim()] = line.slice(eq + 1).trim()
  }
  return { env }
}

export const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/

// Draft -> {payload, name, slot} or {errors:[...]}. Mirrors the server's
// validation so the operator hears about problems before a round trip.
export function buildSpec(draft) {
  const errors = []
  const name = (draft.name || '').trim()
  if (!NAME_RE.test(name)) {
    errors.push("name: 1-32 lowercase letters, digits, '-' or '_'")
  }
  const cmd = parseCommandLine(draft.commandLine)
  if (!cmd) errors.push('command: required — the executable to spawn')
  const envResult = parseEnvLines(draft.envText)
  if (envResult.error) errors.push(`env: ${envResult.error}`)
  const slot = draft.slot === 'guests' ? 'guests' : 'models'
  if (errors.length) return { errors }
  const payload = { command: cmd.command, args: cmd.args }
  if (Object.keys(envResult.env).length) payload.env = envResult.env
  const label = (draft.label || '').trim()
  if (label) payload.label = label
  return { name, slot, payload }
}

// A saved row -> the form draft that would recreate it (edit-in-place).
export function draftFromRow(row) {
  return {
    name: row.name,
    slot: row.slot,
    commandLine: [row.command, ...(row.args || [])].join(' '),
    envText: Object.entries(row.env || {}).map(([k, v]) => `${k}=${v}`).join('\n'),
    label: row.label || '',
  }
}

// What the status dot should say. Order matters: an error outranks a pending
// restart (the running instance TRIED this config and failed), and only the
// models slot ever reports live state — guests spawn per-visit.
export function rowStatus(row) {
  if (row.slot === 'guests') {
    return row.pending_restart
      ? { tone: 'pending', text: 'saved — applies to the next guest after restart' }
      : { tone: 'ok', text: 'available to summoned guests' }
  }
  if (row.error) return { tone: 'error', text: row.error }
  if (row.connected) {
    return { tone: 'ok', text: `connected — ${row.tools.length} tool${row.tools.length === 1 ? '' : 's'}` }
  }
  if (row.pending_restart) return { tone: 'pending', text: 'saved — restart to connect' }
  return { tone: 'error', text: 'not connected' }
}
