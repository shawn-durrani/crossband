- Live transcription comes back on its own after it fails. A network
  blip used to switch the voice session to the slower backup
  transcription until the page was reloaded. It's now tried again after
  10 seconds, then after longer waits if it keeps failing, and the
  banner goes once a turn is transcribed live again. A bad key or a used
  up quota still switches it off for the session.
