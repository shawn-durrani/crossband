- With the calibrated scorer on, the checks on stored voices use it too
  (#523). The hygiene guard sets a stored clip aside when the scorer that
  names voices hears it as someone else, instead of going by the older
  matcher's average fingerprint. A clip's saved score is now the chance
  its voice is that person, and a voice whose human-backed clips have all
  rotated out is paused when its middle clip is under 0.5. A clip saved
  with the older matcher's score is rescored once, after the scorer's
  next build, and keeps its old score beside the new one. The rescore
  never deletes, moves or sets aside a clip. With the scorer off, both
  checks work as before.
