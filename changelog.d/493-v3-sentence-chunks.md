- On Eleven v3, a reply now goes to ElevenLabs a whole sentence at a
  time, so no stretch of speech starts or stops mid-sentence (#493).
  The first word waits for the first sentence to end, which added a
  median of 0.09 to 0.18 seconds in testing. Set
  `tts_v3_sentence_chunks` to `false` to send each piece as the model
  writes it. Every other model is unchanged.
