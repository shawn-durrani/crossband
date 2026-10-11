// The pause that ends your turn in auto voice mode (#621). One home for the
// default, so the voice controller and the settings can't disagree, and for
// what the browser keeps: only a pause the person picked on the slider.
//
// Before #621 the app saved its own 2.0 default on first load, so every
// browser that had used voice held 2.0 and the shorter default from #609
// never applied. A saved 2.0 that nobody picked is that old default, not a
// choice, so it's dropped once. Any other saved value was picked by hand.

export const DEFAULT_PAUSE_SECS = 1.5

const SAVED = 'voice_silence_secs'
const PICKED = 'voice_silence_picked'
const OLD_DEFAULT = 2

// The pause to start with, from what this browser saved.
export function loadPauseSecs(storage) {
  let saved = null
  let picked = false
  try {
    saved = storage.getItem(SAVED)
    picked = storage.getItem(PICKED) === '1'
  } catch {
    return DEFAULT_PAUSE_SECS
  }
  const secs = Number(saved)
  if (!saved || !Number.isFinite(secs) || secs <= 0) return DEFAULT_PAUSE_SECS
  if (secs === OLD_DEFAULT && !picked) {
    try { storage.removeItem(SAVED) } catch { /* nothing to undo */ }
    return DEFAULT_PAUSE_SECS
  }
  return secs
}

// Keep a pause the person picked, marked so a later load never mistakes it
// for the old default.
export function savePauseSecs(storage, secs) {
  try {
    storage.setItem(SAVED, String(secs))
    storage.setItem(PICKED, '1')
  } catch { /* a private window keeps it for this session only */ }
}
