- The room-mode shadow test measures two more changes, and still acts
  on neither. It scores each turn against every clip a person has
  stored, where the live check uses one average of their best three.
  It also says whether a named turn's audio would have been stored if
  each person had a banking bar of their own, set from the scores
  strangers get against that person's voice, with both models agreeing
  when the second model is on. `GET /api/voice/shadow` counts both,
  including the turns a per-person bar would have stored where today's
  bar refused (#477).
