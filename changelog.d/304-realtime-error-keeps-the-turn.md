- A voice turn is no longer lost when realtime transcription fails while
  it's still being transcribed (#304). If the transcription service
  errored after you'd finished speaking but before your words came back,
  the app switched to standard transcription and dropped that turn: red
  errors, then Listening, and someone had to speak again. Now the copy the
  app records alongside is transcribed instead and the turn is sent once,
  even if the realtime words turn up late, and the screen stays on
  Thinking until it's done. A long turn whose last piece comes back empty
  now sends the parts already heard, rather than holding them until
  someone speaks again. On a phone, "save voice diagnostics" is now on the
  call screen: tap the voice line under the agents to find it.
