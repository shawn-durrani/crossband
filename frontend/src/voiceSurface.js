// #69: which voice surface shows. Voice NEVER stops because a panel opened -
// the models menu (or any full page) is just another panel. While a page is
// open, the full call surface gives way to a compact strip that keeps the
// session's controls (mute, end, back) in reach at every width - on desktop
// the in-thread dock unmounts with the chat view, so without the strip a
// live session would show no controls at all, which is how "opening the
// models menu" used to read as "voice ended".
export function voiceSurface({ voiceState, pageOpen }) {
  if (!voiceState || voiceState === 'off') return 'none'
  return pageOpen ? 'strip' : 'call'
}
