// Plain-English messages for voice playback failures. Pure so the node test
// suite covers it. The rule of the house: silent failures must speak, and they
// must speak a language a person can act on.
export function playbackFailureMessage(err) {
  const name = err?.name || ''
  if (name === 'NotAllowedError') {
    return 'The browser blocked audio playback — tap the screen once, then try again.'
  }
  if (name === 'NotSupportedError') {
    return "This browser couldn't decode the reply audio — reload the app and try again."
  }
  return `Voice playback failed${name ? ` (${name})` : ''} — check the silent ` +
         'switch, volume, and Bluetooth routing, then try again.'
}
