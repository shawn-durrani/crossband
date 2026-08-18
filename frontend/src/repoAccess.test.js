// Tests for the repo-access rows (#86).
// Run: node --test frontend/src/repoAccess.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { repoAccessRows, liveSurfaces } from './repoAccess.js'

const ENTRIES = [
  {
    id: 'toolset:github', kind: 'toolset',
    repos: { app: 'alex/app', notes: 'alex/notes' },
  },
  {
    id: 'code:claude_code', kind: 'code',
    guest: { repos: ['app', 'scratch'], writes: true },
  },
  { id: 'mcp:membro-admin', kind: 'mcp', display_name: 'membro-admin', available: true },
  { id: 'memory', kind: 'memory', display_name: 'Memory' },
]

test('rows are the union of both maps, sorted, with per-surface truth', () => {
  const rows = repoAccessRows(ENTRIES)
  assert.deepEqual(rows.map((r) => r.name), ['app', 'notes', 'scratch'])
  const by = Object.fromEntries(rows.map((r) => [r.name, r]))
  // both surfaces
  assert.deepEqual(by.app, { name: 'app', github: 'alex/app', worktree: true, writes: true })
  // GitHub only: no worktree, so the writes switch means nothing here
  assert.deepEqual(by.notes, { name: 'notes', github: 'alex/notes', worktree: false, writes: false })
  // worktree only: the guest can open it, the models' GitHub tools cannot
  assert.deepEqual(by.scratch, { name: 'scratch', github: null, worktree: true, writes: true })
})

test('writes stay false where there is no worktree, whatever the switch says', () => {
  const rows = repoAccessRows([
    { id: 'toolset:github', repos: { app: 'alex/app' } },
    { id: 'code:claude_code', guest: { repos: [], writes: true } },
  ])
  assert.deepEqual(rows, [{ name: 'app', github: 'alex/app', worktree: false, writes: false }])
})

test('live surfaces are the MCP servers, never folded into repo rows', () => {
  assert.deepEqual(liveSurfaces(ENTRIES), [{ name: 'membro-admin', available: true }])
  const names = repoAccessRows(ENTRIES).map((r) => r.name)
  assert.ok(!names.includes('membro-admin'))
})

test('empty or missing entries render nothing rather than throwing', () => {
  assert.deepEqual(repoAccessRows(null), [])
  assert.deepEqual(repoAccessRows([]), [])
  assert.deepEqual(liveSurfaces(undefined), [])
})
