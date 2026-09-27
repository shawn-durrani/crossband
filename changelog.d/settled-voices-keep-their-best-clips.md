- A voice the app knows well now settles and keeps its best clips. Once a
  bank is established, an automatic clip gets in at most once a week, and
  only when it comes from a day the bank doesn't hold yet or beats the
  weakest automatic clip, which it replaces. Established means the
  readiness test says ready when `voice_calibrated_scorer` is on, and
  otherwise enough speech in at least 10 clips from at least two days.
  Clips from an introduction, a correction, a recording or a move always
  go in, and a bank that isn't established learns as before. A refused
  clip is counted with the reason "voice is settled", and the voice
  dock's learning line says "settled".
