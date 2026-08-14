import { useState } from 'react'
import { AudioLines, ChevronRight, ChevronDown, Folder, Settings, Plus, X, Upload, Download, PanelLeftClose, Users, Archive, ArchiveRestore, BarChart3, Plug, Lock } from 'lucide-react'
import { api } from '../api'
import { isChatRunning } from '../runningState'

function ChatRow({ chat, active, running, unread, onSelect, onDelete, onArchive }) {
  const archived = !!chat.archived_at
  return (
    <div
      draggable={!archived}
      onDragStart={(e) => { e.dataTransfer.setData('text/chat-id', String(chat.id)); e.dataTransfer.effectAllowed = 'move' }}
      className={`group flex items-center rounded-lg px-2.5 py-1.5 cursor-pointer text-sm transition-colors ${
        active ? 'bg-panel2 text-ink' : 'text-ink-mid hover:bg-panel hover:text-ink'
      }`}
      style={active ? { boxShadow: 'inset 2px 0 0 var(--t-accent)' } : undefined}
      onClick={() => onSelect(chat.id)}
    >
      {/* Running-task indicator: a pulsing dot when this chat has a round or
          agent working, visible even while you're in another chat. */}
      {running && (
        <span
          className="running-dot shrink-0 mr-1.5"
          role="status"
          aria-label="Task running in this chat"
          title="A task is running in this chat - a model or Claude Code is working."
        />
      )}
      {/* Unread indicator: a message arrived live (a deploy notice, an
          external event, a model reply) while you were looking at another
          chat. Static, not pulsing - running is "something is happening now",
          this is just "something happened, you haven't looked yet". Cleared
          the moment you open the chat. Suppressed when `running` already
          shows a dot, so a chat never shows two at once. */}
      {unread && !running && (
        <span
          className="unread-dot shrink-0 mr-1.5"
          role="status"
          aria-label="New message since you last viewed this chat"
          title="A message arrived in this chat while you were elsewhere."
        />
      )}
      <span className="truncate flex-1">{chat.title}</span>
      {onArchive && (
        <button
          className="opacity-0 group-hover:opacity-100 text-ink-faint hover:text-ink ml-1"
          title={archived ? 'Restore to sidebar' : 'Archive - hide from the sidebar (nothing is deleted)'}
          aria-label={`${archived ? 'Restore' : 'Archive'} chat ${chat.title}`}
          onClick={(e) => { e.stopPropagation(); onArchive(chat.id, !archived) }}
        >
          {archived ? <ArchiveRestore size={13} /> : <Archive size={13} />}
        </button>
      )}
      <button
        className="opacity-0 group-hover:opacity-100 text-ink-faint hover:text-err ml-1"
        title="Delete chat (permanent here; memory keeps what it already learned)"
        aria-label={`Delete chat ${chat.title}`}
        onClick={(e) => {
          e.stopPropagation()
          if (confirm(`Delete chat "${chat.title}"?\n\nThis permanently removes it from this app. Anything the memory service already learned from it (transcript + facts) is kept there. To just hide it, use archive instead.`)) onDelete(chat.id)
        }}
      >
        <X size={13} />
      </button>
    </div>
  )
}

export default function Sidebar({
  projects, chats, activeChatId, runningChats, unreadChats,
  onSelectChat, onNewChat, onNewProject, onDeleteChat, onArchiveChat, onDeleteProject, onEditProject, onMoveChat,
  onManageModels, onOpenIntegrations, onOpenImport, onOpenExport, onOpenCost, onOpenVoices, theme, onToggleTheme, onCollapse,
}) {
  const [collapsed, setCollapsed] = useState({})
  const [showArchived, setShowArchived] = useState(false)
  const [dragOver, setDragOver] = useState(null)
  const dropZone = (target) => ({
    onDragOver: (e) => { e.preventDefault(); if (dragOver !== target) setDragOver(target) },
    onDragLeave: () => setDragOver((d) => (d === target ? null : d)),
    onDrop: (e) => {
      e.preventDefault()
      setDragOver(null)
      const id = Number(e.dataTransfer.getData('text/chat-id'))
      if (id) onMoveChat(id, target === 'standalone' ? null : target)
    },
  })
  const byProject = {}
  const standalone = []
  const archived = []
  for (const c of chats) {
    if (c.archived_at) archived.push(c)  // hidden below, whatever project it's in
    else if (c.project_id) (byProject[c.project_id] ||= []).push(c)
    else standalone.push(c)
  }

  return (
    <aside className="w-72 shrink-0 border-r border-edge bg-app flex flex-col">
      <div className="p-3 flex gap-2 items-center">
        <button
          className="shrink-0 text-ink-dim hover:text-ink p-1"
          title="Collapse sidebar"
          aria-label="Collapse sidebar"
          onClick={onCollapse}
        >
          <PanelLeftClose size={18} />
        </button>
        <button
          className="flex-1 inline-flex items-center justify-center gap-1.5 bg-btn text-btn-ink rounded-lg py-2 text-sm font-semibold hover:bg-btn-hover"
          onClick={() => onNewChat(null)}
        >
          <Plus size={15} /> New chat
        </button>
        <button
          className="inline-flex items-center gap-1.5 border border-edge2 rounded-lg px-3 text-sm text-ink-mid hover:text-ink hover:border-edge3"
          title="New project"
          onClick={onNewProject}
        >
          <Plus size={14} /> Project
        </button>
      </div>
      <div className="flex-1 overflow-y-auto px-2 pb-4 space-y-4">
        {projects.map((p) => (
          <div key={p.id} {...dropZone(p.id)} className={`rounded-lg transition-colors ${dragOver === p.id ? 'bg-panel2/60 ring-1 ring-edge3' : ''}`}>
            <div className="group flex items-center gap-1 px-2 py-1">
              <button
                className="text-ink-dim hover:text-ink-mid flex items-center"
                aria-label={collapsed[p.id] ? `Expand project ${p.name}` : `Collapse project ${p.name}`}
                onClick={() => setCollapsed((c) => ({ ...c, [p.id]: !c[p.id] }))}
              >
                {collapsed[p.id] ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
              </button>
              <span className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.05em] text-ink-dim truncate flex-1">
                <Folder size={13} className="text-ink-dim shrink-0" />
                <span className="truncate">{p.name}</span>
              </span>
              <button
                className="opacity-0 group-hover:opacity-100 text-ink-faint hover:text-ink"
                title="Project settings"
                aria-label={`Settings for project ${p.name}`}
                onClick={() => onEditProject(p)}
              >
                <Settings size={13} />
              </button>
              <button
                className="opacity-0 group-hover:opacity-100 text-ink-faint hover:text-ink"
                title="New chat in project"
                aria-label={`New chat in project ${p.name}`}
                onClick={() => onNewChat(p.id)}
              >
                <Plus size={14} />
              </button>
              <button
                className="opacity-0 group-hover:opacity-100 text-ink-faint hover:text-err"
                title="Delete project"
                aria-label={`Delete project ${p.name}`}
                onClick={() => {
                  if (confirm(`Delete project "${p.name}"? Its chats become standalone.`)) {
                    onDeleteProject(p.id)
                  }
                }}
              >
                <X size={13} />
              </button>
            </div>
            {!collapsed[p.id] && (
              <div className="space-y-0.5 ml-3">
                {(byProject[p.id] || []).map((c) => (
                  <ChatRow
                    key={c.id}
                    chat={c}
                    active={c.id === activeChatId}
                    running={isChatRunning(runningChats, c.id)}
                    unread={isChatRunning(unreadChats, c.id)}
                    onSelect={onSelectChat}
                    onDelete={onDeleteChat}
                    onArchive={onArchiveChat}
                  />
                ))}
                {!(byProject[p.id] || []).length && (
                  <div className="text-xs text-ink-faint px-2.5 py-1">No chats yet</div>
                )}
              </div>
            )}
          </div>
        ))}
        <div {...dropZone('standalone')} className={`rounded-lg transition-colors ${dragOver === 'standalone' ? 'bg-panel2/60 ring-1 ring-edge3' : ''}`}>
          {projects.length > 0 && (
            <div className="text-[11px] font-semibold uppercase tracking-[0.05em] text-ink-dim px-2 py-1">
              Chats
            </div>
          )}
          <div className="space-y-0.5">
            {standalone.map((c) => (
              <ChatRow
                key={c.id}
                chat={c}
                active={c.id === activeChatId}
                running={isChatRunning(runningChats, c.id)}
                unread={isChatRunning(unreadChats, c.id)}
                onSelect={onSelectChat}
                onDelete={onDeleteChat}
                onArchive={onArchiveChat}
              />
            ))}
            {!standalone.length && !projects.length && (
              <div className="text-xs text-ink-faint px-2.5 py-2">
                No chats yet - start one above.
              </div>
            )}
          </div>
        </div>
        {archived.length > 0 && (
          <div className="pt-2">
            <button
              className="w-full flex items-center gap-1 text-[11px] font-semibold uppercase tracking-[0.05em] text-ink-dim px-2 py-1 hover:text-ink"
              aria-expanded={showArchived}
              title="Archived chats are hidden from the sidebar but never deleted"
              onClick={() => setShowArchived((v) => !v)}
            >
              {showArchived ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
              Archived ({archived.length})
            </button>
            {showArchived && (
              <div className="space-y-0.5 ml-3">
                {archived.map((c) => (
                  <ChatRow
                    key={c.id}
                    chat={c}
                    active={c.id === activeChatId}
                    running={isChatRunning(runningChats, c.id)}
                    unread={isChatRunning(unreadChats, c.id)}
                    onSelect={onSelectChat}
                    onDelete={onDeleteChat}
                    onArchive={onArchiveChat}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
      <div className="border-t border-edge p-2 space-y-1.5">
        <button
          className="w-full inline-flex items-center gap-2 text-left text-sm text-ink-mid hover:text-ink rounded-lg px-2.5 py-1.5 hover:bg-panel"
          title="The models in your room - names, personas, voices, and which model each seat runs"
          onClick={onManageModels}
        >
          <Users size={15} className="text-ink-dim" /> Models
        </button>
        <button
          className="w-full inline-flex items-center gap-2 text-left text-sm text-ink-mid hover:text-ink rounded-lg px-2.5 py-1.5 hover:bg-panel"
          title="Who the app can recognise by voice - listen to stored clips, fix names, move recordings, confirm or forget a voice"
          onClick={onOpenVoices}
        >
          <AudioLines size={15} className="text-ink-dim" /> Voices
        </button>
        <button
          className="w-full inline-flex items-center gap-2 text-left text-sm text-ink-mid hover:text-ink rounded-lg px-2.5 py-1.5 hover:bg-panel"
          title="Everything your models can reach - keys, voice, web search, memory, MCP servers - and whether it's working"
          onClick={onOpenIntegrations}
        >
          <Plug size={15} className="text-ink-dim" /> Connections
        </button>
        <button
          className="w-full inline-flex items-center gap-2 text-left text-sm text-ink-mid hover:text-ink rounded-lg px-2.5 py-1.5 hover:bg-panel"
          title="What you've spent, across every chat - metered API billing kept separate from subscription-covered estimates"
          onClick={onOpenCost}
        >
          <BarChart3 size={15} className="text-ink-dim" /> Spend
        </button>
        <div className="flex items-center gap-0.5">
          <button
            className="text-ink-mid hover:text-ink rounded-lg px-2 py-1.5 hover:bg-panel"
            title="Import chats from a Claude or ChatGPT data export"
          aria-label="Import chats"
            onClick={onOpenImport}
          >
            <Upload size={16} />
          </button>
          <button
            className="text-ink-mid hover:text-ink rounded-lg px-2 py-1.5 hover:bg-panel"
            title="Export chats as markdown/JSON (paste back into Claude or ChatGPT)"
          aria-label="Export chats"
            onClick={onOpenExport}
          >
            <Download size={16} />
          </button>
          <button
            className="text-ink-mid hover:text-ink rounded-lg px-2 py-1.5 hover:bg-panel"
            title="Lock this browser - you'll unlock with your passkey or password (set one under Connections)"
            aria-label="Lock this browser"
            onClick={async () => { try { await api.authLogout() } finally { location.reload() } }}
          >
            <Lock size={16} />
          </button>
          <select
            className="ml-auto text-xs bg-app text-ink-mid border border-edge rounded-lg px-2 py-1.5 hover:border-edge2 cursor-pointer"
            title="Theme"
            aria-label="Theme"
            value={theme}
            onChange={(e) => onToggleTheme(e.target.value)}
          >
            <option value="dark">Zinc</option>
            <option value="midnight">Midnight</option>
            <option value="espresso">Espresso</option>
            <option value="oled">OLED</option>
            <option value="light">Light</option>
          </select>
        </div>
      </div>
    </aside>
  )
}
