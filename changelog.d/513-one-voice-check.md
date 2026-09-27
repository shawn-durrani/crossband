- Every spoken turn is named one way now, in every mode (#513). The app
  follows each voice through the voice session on your computer, names
  each voice from everything it has said, with the calibrated scorer
  once it's ready, and names every turn before the AIs read it. A voice
  named later fills in its earlier turns, a new voice is asked about
  once, and naming a turn by hand names its voice for the rest of the
  session. With no diariser set, or with it down, each turn is named on
  its own. The settings `voice_session_shadow`, `voice_session_labels`,
  `voice_session_live`, `voice_session_only` and `voice_id_pending_extra`
  are gone: what the switches turned on always runs, and a config file
  that still names them loads as before. With `voice_id_enabled` off, no
  spoken turn is checked or named.
