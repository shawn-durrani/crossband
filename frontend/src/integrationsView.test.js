// Tests for the Integrations console's pure presentation layer. These pin the
// things a wrong build would get wrong: sectioning, the plain-English health
// readout for every state (configured / missing-key / unhealthy / trial /
// onboarded), and the two SAFETY invariants. A trial seat is never routinely
// selectable, and onboarding stays gated on known cost.
// Run: node --test frontend/src/integrationsView.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  SECTIONS,
  groupIntegrations, healthPresentation, offersCredentialFix, provenanceLabel,
  seatOnboardable, routinelySelectable, seatBadge, seatPromoteState, credentialPrompt, configHowTo } from './integrationsView.js'

const entry = (over) => ({
  id: 'x', kind: 'search', health: 'unknown', seats: [], ...over,
})
const seat = (over) => ({
  slug: 's', name: 'S', model: 'm', enabled: true, lifecycle: 'trial',
  cost_provenance: { source: 'unknown' }, eligible_for_auto_selection: false, ...over,
})

// ---------- grouping ----------

test('groups entries into ordered capability sections; drops empty ones', () => {
  const secs = groupIntegrations([
    entry({ id: 'anthropic', kind: 'llm' }),
    entry({ id: 'elevenlabs', kind: 'audio' }),
    entry({ id: 'tavily', kind: 'search' }),
    entry({ id: 'memory', kind: 'memory' }),
    // no mcp entries → the MCP section must not appear
  ])
  assert.deepEqual(secs.map((s) => s.kind), ['llm', 'audio', 'search', 'memory'])
  assert.equal(secs[0].title, 'AI models')
  assert.equal(secs[0].entries.length, 1)
})

test('an MCP server present makes the read-only MCP section appear last', () => {
  const secs = groupIntegrations([
    entry({ id: 'anthropic', kind: 'llm' }),
    entry({ id: 'mcp:x', kind: 'mcp', health: 'healthy' }),
  ])
  assert.deepEqual(secs.map((s) => s.kind), ['llm', 'mcp'])
})

test('empty input yields no sections (never a wall of empty headings)', () => {
  assert.deepEqual(groupIntegrations([]), [])
  assert.deepEqual(groupIntegrations(undefined), [])
})

// ---------- health readout ----------

test('healthy is calm and not actionable', () => {
  const p = healthPresentation(entry({ health: 'healthy' }))
  assert.equal(p.tone, 'ok')
  assert.equal(p.actionable, false)
})

test('unhealthy reads as needs-attention and is actionable', () => {
  const p = healthPresentation(entry({ health: 'unhealthy' }))
  assert.equal(p.label, 'Needs attention')
  assert.equal(p.tone, 'err')
  assert.equal(p.actionable, true)
})

test('a missing key is a warn that the owner can act on', () => {
  const p = healthPresentation(entry({ health: 'unconfigured', kind: 'search' }))
  assert.equal(p.label, 'Not set up')
  assert.equal(p.tone, 'warn')
  assert.equal(p.actionable, true)
})

test('memory absence is calm, not an alarm (it is optional by design)', () => {
  const p = healthPresentation(entry({ health: 'unconfigured', kind: 'memory' }))
  assert.equal(p.label, 'Not detected')
  assert.equal(p.tone, 'muted')
  assert.equal(p.actionable, false)
})

test('a pending MCP handshake reads as connecting, not failed', () => {
  const p = healthPresentation(entry({ health: 'unknown', kind: 'mcp' }))
  assert.equal(p.label, 'Connecting…')
  assert.equal(p.tone, 'muted')
})

test('configured-but-unchecked is muted and non-alarming', () => {
  const p = healthPresentation(entry({ health: 'unknown', kind: 'search' }))
  assert.equal(p.tone, 'muted')
  assert.equal(p.actionable, false)
})

// ---------- credential-fix shortcut ----------

test('credential fix is offered for missing/unhealthy keyed capabilities only', () => {
  assert.equal(offersCredentialFix(entry({ kind: 'llm', health: 'unconfigured' })), true)
  assert.equal(offersCredentialFix(entry({ kind: 'audio', health: 'unhealthy' })), true)
  assert.equal(offersCredentialFix(entry({ kind: 'llm', health: 'healthy' })), false)
  // memory has no key; MCP is read-only here — neither offers a key fix
  assert.equal(offersCredentialFix(entry({ kind: 'memory', health: 'unconfigured' })), false)
  assert.equal(offersCredentialFix(entry({ kind: 'mcp', health: 'unhealthy' })), false)
})

// ---------- provenance labels ----------

test('provenance labels are plain-English and default to unknown', () => {
  assert.match(provenanceLabel('rate_card_estimate'), /Rate-card estimate/)
  assert.match(provenanceLabel('self_hosted_zero_marginal'), /Self-hosted/)
  assert.match(provenanceLabel('provider_reported'), /billed/i)
  assert.match(provenanceLabel('nonsense'), /Unknown/)
})

// ---------- seat lifecycle + onboarding gate ----------

test('a seat with unknown provenance is not onboardable', () => {
  assert.equal(seatOnboardable(seat({ cost_provenance: { source: 'unknown' } })), false)
  assert.equal(seatOnboardable(seat({ cost_provenance: {} })), false)
})

test('a seat with a known provenance is onboardable', () => {
  assert.equal(seatOnboardable(seat({ cost_provenance: { source: 'rate_card_estimate' } })), true)
})

test('SAFETY: a trial seat is never routinely/default selectable', () => {
  assert.equal(routinelySelectable(seat({ lifecycle: 'trial' })), false)
  // even a trial seat that somehow carried an eligibility flag stays excluded
  assert.equal(routinelySelectable(seat({ lifecycle: 'trial', eligible_for_auto_selection: true })), false)
})

test('only an onboarded, auto-eligible seat is routinely selectable', () => {
  assert.equal(routinelySelectable(seat({ lifecycle: 'onboarded', eligible_for_auto_selection: true })), true)
  // onboarded but not eligible (unknown cost) is still excluded
  assert.equal(routinelySelectable(seat({ lifecycle: 'onboarded', eligible_for_auto_selection: false })), false)
})

test('badge distinguishes trial (loud) from onboarded (calm)', () => {
  assert.equal(seatBadge(seat({ lifecycle: 'trial' })).tone, 'trial')
  assert.match(seatBadge(seat({ lifecycle: 'trial' })).title, /manual-invoke only/i)
  assert.equal(seatBadge(seat({ lifecycle: 'onboarded' })).tone, 'onboarded')
})

test('promote is hidden for an onboarded seat', () => {
  assert.equal(seatPromoteState(seat({ lifecycle: 'onboarded' })).show, false)
})

test('trial + known cost → promote enabled with the provenance reason', () => {
  const s = seatPromoteState(seat({ cost_provenance: { source: 'rate_card_estimate' } }))
  assert.equal(s.show, true)
  assert.equal(s.enabled, true)
  assert.match(s.reason, /Rate-card estimate/)
})

test('trial + unknown cost → promote shown but disabled, explaining why', () => {
  const s = seatPromoteState(seat({ cost_provenance: { source: 'unknown' } }))
  assert.equal(s.show, true)
  assert.equal(s.enabled, false)
  assert.match(s.reason, /no cost provenance/i)
  assert.match(s.reason, /@mention|by name/i) // still reachable manually
})

// ---------- Web-research two-row collapse ----------
// The backend keeps Tavily and Brave as separate, individually-fixable entries;
// the console collapses the {Tavily, Brave} any-of group into ONE "Web search"
// row while leaving Reddit as its own untouched row. So the Web research section
// always renders exactly TWO rows — never one, never three — and Reddit's own
// configuration must never leak into the Web-search row's satisfied state.

const webEngine = (id, name, satisfied, health) => entry({
  id, kind: 'search', display_name: name, health,
  requires: [{
    type: 'credential', label: name, env: [`${id.toUpperCase()}_API_KEY`],
    optional: true, satisfied, setup_service: id, any_of: 'web_search',
  }],
})
const tavily = (satisfied, health) => webEngine('tavily', 'Tavily Web Search', satisfied, health)
const brave = (satisfied, health) => webEngine('brave', 'Brave Web Search', satisfied, health)
const reddit = (satisfied = true, health = 'healthy') => entry({
  id: 'reddit', kind: 'search', display_name: 'Reddit Research', health,
  requires: [{
    type: 'credential', label: 'Reddit Research', env: ['REDDIT_CLIENT_ID'],
    optional: true, satisfied, setup_service: 'reddit', any_of: null,
  }],
})
const searchSection = (entries) =>
  groupIntegrations(entries).find((s) => s.kind === 'search')

test('collapse: zero keys → one unconfigured Web search row + Reddit (two rows)', () => {
  const sec = searchSection([
    tavily(false, 'unconfigured'), brave(false, 'unconfigured'), reddit(false, 'unknown'),
  ])
  assert.equal(sec.entries.length, 2)
  const web = sec.entries[0]
  assert.equal(web.id, 'web-search')
  assert.equal(web.display_name, 'Web search')
  assert.equal(web.configured, false)
  assert.equal(web.available, false)
  assert.equal(web.health, 'unconfigured')
  // the detail explains page + Reddit fetch still work with no key
  assert.match(web.detail, /page/i)
  assert.match(web.detail, /reddit/i)
})

test('collapse: Tavily only → collapsed row is satisfied/available/healthy', () => {
  const sec = searchSection([
    tavily(true, 'healthy'), brave(false, 'unconfigured'), reddit(),
  ])
  assert.equal(sec.entries.length, 2)
  const web = sec.entries[0]
  assert.equal(web.id, 'web-search')
  assert.equal(web.configured, true)
  assert.equal(web.available, true)
  assert.equal(web.health, 'healthy')
})

test('collapse: Brave only → collapsed row is satisfied/available/healthy', () => {
  const sec = searchSection([
    tavily(false, 'unconfigured'), brave(true, 'healthy'), reddit(),
  ])
  assert.equal(sec.entries.length, 2)
  const web = sec.entries[0]
  assert.equal(web.id, 'web-search')
  assert.equal(web.configured, true)
  assert.equal(web.available, true)
  assert.equal(web.health, 'healthy')
})

test('collapse: both keys → satisfied, engines string names Tavily AND Brave', () => {
  const sec = searchSection([
    tavily(true, 'healthy'), brave(true, 'healthy'), reddit(),
  ])
  assert.equal(sec.entries.length, 2)
  const web = sec.entries[0]
  assert.equal(web.configured, true)
  assert.equal(web.available, true)
  assert.match(web.detail, /Tavily/)
  assert.match(web.detail, /Brave/)
})

test('collapse: Reddit configured does NOT make Web search read satisfied; Reddit stays its own row', () => {
  const sec = searchSection([
    tavily(false, 'unconfigured'), brave(false, 'unconfigured'), reddit(true, 'healthy'),
  ])
  assert.equal(sec.entries.length, 2)
  const web = sec.entries[0]
  assert.equal(web.id, 'web-search')
  assert.equal(web.configured, false)      // Reddit is not part of the {Tavily,Brave} group
  assert.equal(web.available, false)
  assert.equal(web.health, 'unconfigured')
  // Reddit renders untouched as its own second row
  const rd = sec.entries[1]
  assert.equal(rd.id, 'reddit')
  assert.equal(rd.display_name, 'Reddit Research')
  assert.equal(rd.health, 'healthy')
  assert.equal(rd.requires[0].satisfied, true)
  assert.equal(rd.requires[0].any_of, null)
})

test('collapse: health follows best-engine-wins when one engine is unhealthy', () => {
  const sec = searchSection([
    tavily(true, 'unhealthy'), brave(true, 'healthy'), reddit(),
  ])
  assert.equal(sec.entries.length, 2)
  const web = sec.entries[0]
  assert.equal(web.configured, true)
  assert.equal(web.health, 'healthy')      // the best engine wins, not the worst
})

// ── the room + abilities sections ───────────────────────────────────────────
// Three shipped capabilities appeared in no section at all, so the console
// couldn't show their health and nothing listed what the room can actually do.

test('the new kinds each have a section, in a sensible reading order', () => {
  const kinds = SECTIONS.map((s) => s.kind)
  for (const k of ['code', 'toolset', 'channel']) assert.ok(kinds.includes(k), k)
  // who is in the room, then what they can do, then what is connected
  assert.ok(kinds.indexOf('code') < kinds.indexOf('mcp'))
  assert.ok(kinds.indexOf('llm') < kinds.indexOf('code'))
})

test('every section carries a plain-English blurb', () => {
  // Comprehension is accessibility: a section a non-technical owner can't
  // interpret is a section that may as well not be there.
  for (const s of SECTIONS) {
    assert.ok(s.title && s.title.length > 2, s.kind)
    assert.ok(s.blurb && s.blurb.length > 20, s.kind)
  }
})

test('the new capabilities are sectioned, not dropped on the floor', () => {
  const entries = [
    { id: 'code:claude_code', kind: 'code', display_name: 'Claude Code guest', health: 'unconfigured' },
    { id: 'toolset:github', kind: 'toolset', display_name: 'GitHub tools', health: 'healthy' },
    { id: 'channel:ingest', kind: 'channel', display_name: 'Incoming events', health: 'healthy' },
  ]
  const placed = groupIntegrations(entries).flatMap((s) => s.entries.map((e) => e.id))
  for (const e of entries) assert.ok(placed.includes(e.id), e.id)
})

test('sections come from the server when supplied', () => {
  // A new kind must need no change here — the hand-written list was the last
  // hardcoded site and the reason the console read as a catalogue.
  const entries = [{ id: 'x:1', kind: 'weather', health: 'healthy', seats: [] }]
  const fromServer = [{ kind: 'weather', title: 'Weather', blurb: 'A kind this file has never heard of.' }]
  const out = groupIntegrations(entries, fromServer)
  assert.equal(out.length, 1)
  assert.equal(out[0].title, 'Weather')
  assert.equal(out[0].entries[0].id, 'x:1')
})

test('falls back to the built-in list when the server sends none', () => {
  const entries = [{ id: 'anthropic', kind: 'llm', health: 'healthy', seats: [] }]
  assert.equal(groupIntegrations(entries, null)[0].kind, 'llm')
  assert.equal(groupIntegrations(entries, [])[0].kind, 'llm')
})

test('an unconfigured capability yields an onboarding prompt from its own requirements', () => {
  const e = {
    kind: 'llm', health: 'unconfigured',
    requires: [{ type: 'credential', env: ['ACME_API_KEY'], satisfied: false, setup_service: 'acme' }],
  }
  const p = credentialPrompt(e)
  assert.equal(p.service, 'acme')
  assert.equal(p.fields.length, 1)
  assert.equal(p.fields[0].role, 'key')
  assert.equal(p.label, 'Add key')
})

test('a two-part credential asks for both halves', () => {
  const p = credentialPrompt({
    kind: 'search', health: 'unconfigured',
    requires: [{ type: 'credential', env: ['R_CLIENT_ID', 'R_SECRET'], satisfied: false, setup_service: 'reddit' }],
  })
  assert.deepEqual(p.fields.map((f) => f.role), ['key', 'secret'])
})

test('nothing is asked for when there is nothing to fix', () => {
  const req = [{ type: 'credential', env: ['K'], satisfied: true, setup_service: 's' }]
  assert.equal(credentialPrompt({ kind: 'llm', health: 'healthy', requires: req }), null)
  // memory is a service you run and MCP is a file you edit — neither is a key
  assert.equal(credentialPrompt({ kind: 'memory', health: 'unconfigured', requires: req }), null)
  assert.equal(credentialPrompt({ kind: 'mcp', health: 'unhealthy', requires: req }), null)
  assert.equal(credentialPrompt(null), null)
})

test('unhealthy asks to replace rather than add', () => {
  const p = credentialPrompt({
    kind: 'audio', health: 'unhealthy',
    requires: [{ type: 'credential', env: ['X_KEY'], satisfied: false, setup_service: 'x' }],
  })
  assert.equal(p.label, 'Replace key')
})

// Secret-scan discipline for the snippets below (scripts/secret-scan.sh). A
// snippet ships to every reader's screen, so it must be built out of the
// documented placeholder vocabulary and nothing else: no home path outside the
// placeholder root, no tailnet host, and none of the real project names that
// were sitting in front of whoever wrote the example.
//
// Every check here is hardcoded, so the suite gives the same verdict on every
// machine. Deriving the forbidden value from the account running the tests
// would make the result depend on who ran it, and CI (where that account is
// something like `runner`) would quietly assert nothing.
const HOME_PATH = /\/(?:Users|home)\/[^"\s]*/g
// Anchored like ALLOW_HOME in scripts/secret-scan.sh, and one notch stricter:
// the whole segment after /Users has to BE a placeholder word, so
// `/Users/you-really` is still a leak even though it opens with an allowed
// one. A snippet is a string we author, so it can be held to that.
const PLACEHOLDER_HOME = /^\/(?:Users|home)\/(?:you|user|username|name|me|example|runner|<[a-z-]+>)(?:\/|$)/
const TAILNET_HOST = /[A-Za-z0-9<>_.-]+\.ts\.net/
// Neutral tokens only. These are this project's own names and the sibling
// service it talks to, all of which already ship all over this tree, so
// writing them down costs nothing. An owner's name, account, or private repo
// is the very thing a deny-list exists to keep out, and spelling one here to
// forbid it would publish it just as surely as the snippet would have.
const FORBIDDEN_TOKENS = ['sideband', 'crossband', 'membro']

test('config-file capabilities teach in place', () => {
  // The dead-end this closes: "add code_repos to config.local.json" with no
  // link and no example, on a page with exactly one link. Every config-file
  // kind must carry a copy-paste snippet that is VALID JSON, says where the
  // file lives, and points at the full reference.
  for (const kind of ['code', 'mcp', 'channel']) {
    const how = configHowTo({ kind })
    assert.ok(how, `${kind} must have a how-to`)
    assert.doesNotThrow(() => JSON.parse(how.snippet), `${kind} snippet must be valid JSON`)
    assert.match(how.intro + how.outro, /config\.local\.json|\/api\/ingest/)
    assert.match(how.outro, /docs\/CONFIG\.md/)
    // synthetic placeholders only: never a path, host or name off a real machine
    for (const p of how.snippet.match(HOME_PATH) || []) {
      assert.match(p, PLACEHOLDER_HOME, `${kind} snippet has a real home path: ${p}`)
    }
    assert.doesNotMatch(how.snippet, TAILNET_HOST, `${kind} snippet names a tailnet host`)
    for (const token of FORBIDDEN_TOKENS) {
      assert.ok(!how.snippet.toLowerCase().includes(token),
        `${kind} snippet uses the real project name "${token}" where a placeholder belongs`)
    }
  }
  // in-app-affordance kinds get no snippet — they have real buttons
  assert.equal(configHowTo({ kind: 'llm' }), null)
  assert.equal(configHowTo({ kind: 'audio' }), null)
  assert.equal(configHowTo(null), null)
})
