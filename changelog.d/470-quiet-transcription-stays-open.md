- Live transcription stays connected through quiet stretches.
  ElevenLabs closes a realtime connection that hears nothing for about
  15 seconds, so the app used to reconnect after every pause, and a
  turn spoken just then lost its start or took the slower backup way.
  The app now sends a sliver of silence while the room is quiet. If the
  connection is replaced mid-sentence anyway, the turn is sent whole
  from the backup recording. The log says why each backup turn happened.
