- The shadow test can now try the new way of naming voices (#482). Set
  `voice_session_shadow` and it follows each voice through the whole
  voice session, names each voice from everything it has said, and
  writes what it would have named beside today's label. It changes
  nothing you see, and `GET /api/voice/shadow/sessions` shows the two
  side by side.
