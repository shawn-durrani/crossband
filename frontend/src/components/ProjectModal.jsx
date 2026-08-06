import { useState } from 'react'

export default function ProjectModal({ project, onSave, onClose }) {
  const [name, setName] = useState(project?.name || '')
  const [instructions, setInstructions] = useState(project?.instructions || '')
  const [memory, setMemory] = useState(project?.memory || '')
  const isNew = !project?.id

  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-6"
      onClick={onClose}
    >
      <div
        className="bg-panel border border-edge2 rounded-2xl w-full max-w-2xl p-6 space-y-4 max-h-[85vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold">{isNew ? 'New project' : 'Project settings'}</h2>
        <label className="block">
          <span className="text-sm text-ink-mid">Name</span>
          <input
            className="mt-1 w-full bg-app border border-edge2 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-edge3"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. App launch planning"
            autoFocus
          />
        </label>
        <label className="block">
          <span className="text-sm text-ink-mid">
            Project instructions — injected into both models in every chat in this project
          </span>
          <textarea
            className="mt-1 w-full bg-app border border-edge2 rounded-lg px-3 py-2 text-sm h-28 focus:outline-none focus:border-edge3"
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            placeholder="e.g. This project is about… Be concise. Claude plays devil's advocate."
          />
        </label>
        {!isNew && (
          <label className="block">
            <span className="text-sm text-ink-mid">
              Project memory — auto-distilled from chats; edit or prune freely
            </span>
            <textarea
              className="mt-1 w-full bg-app border border-edge2 rounded-lg px-3 py-2 text-sm h-40 font-mono focus:outline-none focus:border-edge3"
              value={memory}
              onChange={(e) => setMemory(e.target.value)}
            />
          </label>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <button
            className="border border-edge2 rounded-lg px-4 py-2 text-sm text-ink-mid hover:border-edge3"
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            className="bg-btn text-btn-ink rounded-lg px-4 py-2 text-sm font-semibold hover:bg-btn-hover disabled:opacity-40"
            disabled={!name.trim()}
            onClick={() => onSave({ name: name.trim(), instructions, memory })}
          >
            {isNew ? 'Create' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}
