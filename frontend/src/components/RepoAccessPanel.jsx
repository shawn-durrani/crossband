import { Check, Minus } from 'lucide-react'
import { repoAccessRows, liveSurfaces } from '../repoAccess'

// Repo access panel (#86): what each agent surface can actually reach, per
// repo, in plain English - rendered from the same live status payload as the
// rest of the Connections console. The three kinds of access the room kept
// conflating get one column or note each: GitHub's copy (issues/PRs), an
// isolated worktree on this machine, and live-machine MCP surfaces. The
// backend re-reads the maps on every request, so edits to config.local.json
// show up here without a restart.

function Cell({ on, label, title }) {
  if (!on) {
    return (
      <span className="inline-flex items-center gap-1 text-ink-faint" title="No access">
        <Minus size={12} /> no
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1 text-ok" title={title}>
      <Check size={12} /> {label}
    </span>
  )
}

export default function RepoAccessPanel({ entries }) {
  const rows = repoAccessRows(entries)
  const surfaces = liveSurfaces(entries)
  if (!rows.length) return null
  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.06em] text-ink-dim">
          Repo access
        </h2>
        <p className="text-xs text-ink-faint mt-0.5">
          What each agent surface can reach, per repo. Live from
          {' '}<span className="font-mono">config.local.json</span> - edits apply
          here (and to the tools) without a restart.
        </p>
      </div>
      <div className="border border-edge2 rounded-xl bg-panel p-4 space-y-3">
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-ink-dim">
                <th className="font-medium pb-2 pr-3">Repo</th>
                <th className="font-medium pb-2 pr-3">GitHub tools</th>
                <th className="font-medium pb-2">Coding guest</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.name} className="border-t border-edge align-top">
                  <td className="py-2 pr-3 font-mono font-medium text-ink-mid">{r.name}</td>
                  <td className="py-2 pr-3">
                    {r.github ? (
                      <span className="inline-flex items-center gap-1 text-ok"
                        title="The models can read and write issues and pull requests here, each write signed with which AI did it.">
                        <Check size={12} /> <span className="font-mono text-ink-mid">{r.github}</span>
                      </span>
                    ) : (
                      <Cell on={false} />
                    )}
                  </td>
                  <td className="py-2">
                    <Cell
                      on={r.worktree}
                      label={r.writes ? 'read + write' : 'read only'}
                      title={r.writes
                        ? 'Claude Code works in an isolated copy on this machine and may branch, test and open a pull request. It never edits your live checkout.'
                        : 'Claude Code reads an isolated copy on this machine. Writes are switched off (code_allow_writes).'}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="text-xs text-ink-faint leading-relaxed space-y-1">
          <p>
            <span className="text-ink-mid font-medium">GitHub tools</span> touch
            GitHub&apos;s copy of a repo (issues and pull requests), never the files on
            this machine. <span className="text-ink-mid font-medium">Coding guest</span>{' '}
            means Claude Code can be summoned into an isolated copy of the repo here;
            it opens pull requests rather than editing your live checkout.
          </p>
          <p>
            {surfaces.length > 0 ? (
              <>
                <span className="text-ink-mid font-medium">Live surfaces</span> are not
                repo-scoped: the MCP server{surfaces.length === 1 ? '' : 's'}{' '}
                {surfaces.map((s) => s.name).join(', ')} reach{surfaces.length === 1 ? 'es' : ''} live
                services on this machine, listed under MCP servers below.
              </>
            ) : (
              <>No live-machine surfaces are configured: no MCP servers reach beyond the repos above.</>
            )}
          </p>
        </div>
      </div>
    </section>
  )
}
