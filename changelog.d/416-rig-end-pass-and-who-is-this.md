- The voice rig now scores the naming pass that ends a voice session,
  and the answers to "who's this?" (#416). Its second copy of the app
  ends a voice session after 45 quiet seconds, through a new
  `voice_session_idle_s` setting that stays at 10 minutes for you, and
  the report sets the names after that pass beside the names when the
  conversation ended. Two new scripts let a voice nobody knows talk
  until the app asks who it is: in one the owner says it's a TV, and in
  the other names the person and spells the name. The report says
  whether the right turn took the answer, the voice's earlier turns were
  relabelled and a clip was saved. Anyone the app meets in a
  conversation is forgotten when it ends, so every conversation starts
  knowing the same people.
