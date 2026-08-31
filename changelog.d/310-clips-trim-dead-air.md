- Stored voice clips no longer carry their dead air (#310). Every
  capture starts with pre-roll and ends with the pause that ends a
  turn; that silence was stored, it diluted the voice embedding, and
  it counted toward the seconds a voice needs before identification
  is trusted. Clips are now trimmed to their voiced span at banking,
  with a small margin, and a noisy tail trims like a silent one. Gaps
  inside the speech are untouched. Clips restored from membro keep
  membro's own content address, so owner corrections and the sync
  keep speaking to the durable copy rather than minting variants.
