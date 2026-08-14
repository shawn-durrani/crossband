// Plain-English messages for voice playback failures. Pure so the node test
// suite covers it. The rule of the house: silent failures must speak, and they
// must speak a language a person can act on.
export function playbackFailureMessage(err, { mobile = false } = {}) {
  const name = err?.name || ''
  if (name === 'NotAllowedError') {
    return 'The browser blocked audio playback - tap the screen once, then try again.'
  }
  if (name === 'NotSupportedError') {
    return "This browser couldn't decode the reply audio - reload the app and try again."
  }
  if (name === 'AbortError') {
    // Loading a new reply (or an interruption) aborts the previous play() -
    // routine after a barge-in, not a device problem. #21: this used to
    // print the mobile silent-switch checklist on a desktop Mac.
    return 'Playback was cut off mid-reply - normal after an interruption. ' +
           'If audio stays silent, check the output device and volume.'
  }
  return `Voice playback failed${name ? ` (${name})` : ''} - check ` +
         (mobile ? 'the silent switch, volume, and Bluetooth routing, then try again.'
                 : 'the output device, volume, and any Bluetooth routing, then try again.')
}
