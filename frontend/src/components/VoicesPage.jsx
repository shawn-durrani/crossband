// #91: all voice-identity capability in one first-class place, linked from
// the sidebar - people management, clip audition and reassignment, the
// audition prompts (#83), learning progress and hygiene state. The models
// menu's remembered-voices section shrank to a summary line pointing here;
// RememberedVoices itself is the same battle-tested panel, hosted with the
// room a full page gives it.
import { AudioLines, Menu, X } from 'lucide-react'
import RememberedVoices from './RememberedVoices'

export default function VoicesPage({ onClose, onOpenMenu }) {
  return (
    <div className="flex-1 min-h-0 overflow-y-auto">
      <div className="mx-auto w-full max-w-3xl px-4 sm:px-6 py-6 space-y-5">
        <header className="flex items-start gap-3">
          {onOpenMenu && (
            <button className="sm:hidden text-ink-mid hover:text-ink p-1 -ml-1 shrink-0"
              aria-label="Open chats & settings" onClick={onOpenMenu}>
              <Menu size={20} />
            </button>
          )}
          <div className="flex-1 min-w-0">
            <h1 className="text-lg font-semibold flex items-center gap-2">
              <AudioLines size={18} className="text-ink-dim" /> Voices
            </h1>
            <p className="text-sm text-ink-mid mt-0.5">
              Who this app can recognise by voice, what each voice was learnt
              from, and every control over that: listen to the stored clips,
              fix names and spellings, move a recording to the right person,
              confirm a bank you have auditioned, or forget someone entirely.
              Nothing here ever leaves this machine except inside your own
              room-mode transcription requests.
            </p>
          </div>
          <button
            className="text-xs inline-flex items-center gap-1 rounded-full px-2 py-1.5 border border-edge2 text-ink-dim hover:text-ink shrink-0"
            onClick={onClose}
          >
            <X size={13} /> Back to chat
          </button>
        </header>
        <RememberedVoices />
      </div>
    </div>
  )
}
