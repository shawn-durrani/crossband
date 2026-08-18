// Repo access rows for the Connections console (#86).
//
// Two days of field evidence behind this: the repo maps lived in config only,
// were wrong for days, and the room repeatedly asserted contradictory access
// facts because nothing displayed them. This module is the pure translation
// from the unified status payload (GET /api/integrations) to one table that
// distinguishes the three kinds of access the room kept conflating:
//
//   github   - the models read/write issues and PRs on GitHub's copy
//   worktree - the coding guest works in an isolated copy on this machine
//   live     - MCP servers touch live services, and are not repo-scoped
//
// Kept React-free and side-effect-free (the integrationsView.js discipline)
// so the meaning is unit-tested directly. The backend re-reads the maps per
// request (#24/#86), so what this renders is live config, not a boot snapshot.

export function repoAccessRows(entries) {
  const gh = (entries || []).find((e) => e.id === 'toolset:github')
  const guest = (entries || []).find((e) => e.id === 'code:claude_code')
  const ghMap = gh?.repos || {}
  const guestRepos = guest?.guest?.repos || []
  const writes = !!guest?.guest?.writes
  const names = [...new Set([...Object.keys(ghMap), ...guestRepos])].sort()
  return names.map((name) => ({
    name,
    // owner/repo when the models' GitHub tools reach it, else null
    github: ghMap[name] || null,
    // true when the coding guest can be summoned into a worktree of it
    worktree: guestRepos.includes(name),
    // writes are a single global switch, meaningful only where a worktree is
    writes: guestRepos.includes(name) && writes,
  }))
}

// MCP servers, named as the live-machine surfaces they are. Deliberately NOT
// merged into the repo rows: pretending a live surface is scoped to a repo is
// exactly the conflation the panel exists to end.
export function liveSurfaces(entries) {
  return (entries || [])
    .filter((e) => e.kind === 'mcp')
    .map((e) => ({ name: e.display_name, available: !!e.available }))
}
