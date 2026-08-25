// Clip audition derivations (#68) - pure, node --test'd, no DOM.
//
// The rule this module owns: every clip row must say, in plain English,
// HOW the app came to store that audio - because the whole point of the
// panel is judging whether the capture path can be trusted by ear.

// How a clip was earned -> what the row says. Verbatim source strings from
// the anchor store; anything unknown renders as itself rather than a guess.
const SOURCE_LABELS = {
  accumulated: 'captured live',
  'cold-start': 'learnt by elimination',
  introduction: 'from an introduction',
  correction: 'from your correction',
  'harvested-short': 'short interjection sample',
}

export function sourceLabel(source) {
  return SOURCE_LABELS[source] || String(source || 'unknown')
}

// A clip earned by elimination was never voice-verified - it is the capture
// path both phantom banks abused - so the row flags it for a closer listen.
export function needsEar(clip) {
  return clip.source === 'cold-start'
}

// Why a clip was set aside -> [chip, tooltip] (#219). The reason decides
// the remedy: a contaminated clip may belong to the other person (move
// it), a noise clip belongs to nobody (delete it) - so the row must not
// blur the two. A reasonless quarantine predates reasons and reads as the
// original pairwise verdict.
const QUARANTINE_COPY = {
  contaminated: ['set aside',
    'Set aside by the hygiene audit - it sounded more like another '
    + 'remembered voice. Stored but not used for matching.'],
  not_speech: ['not a voice',
    'Set aside by the hygiene audit - no speech was heard in this '
    + 'recording. Stored but not used for matching.'],
}

export function quarantineCopy(clip) {
  if (!clip.quarantined) return ['', '']
  return QUARANTINE_COPY[clip.quarantine_reason] || QUARANTINE_COPY.contaminated
}

export function clipRow(clip) {
  const secs = Number(clip.seconds || 0)
  const [qChip, qTitle] = quarantineCopy(clip)
  return {
    file: clip.file,
    source: clip.moved ? 'reassigned by you' : sourceLabel(clip.source),
    needsEar: needsEar(clip) && !clip.moved,
    quarantined: Boolean(clip.quarantined),
    quarantineChip: qChip,
    quarantineTitle: qTitle,
    duration: `${secs.toFixed(1)}s`,
    when: clip.added_at
      ? new Date(clip.added_at * 1000).toLocaleString([], {
          day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
        })
      : '',
  }
}

// Where a clip may be refiled (#90): every OTHER remembered person, by
// display name - the row's own person is never a target.
export function moveTargets(people, currentPersonId) {
  return (people || [])
    .filter((p) => p.person_id !== currentPersonId)
    .map((p) => ({ person_id: p.person_id,
                   name: p.preferred_name || p.name }))
}

export const DELETE_CLIP_EXPLAINER =
  'Deletes this one recording from disk. The voice re-learns from what ' +
  'remains; deleting the last clip leaves the person known but unlearnt.'
