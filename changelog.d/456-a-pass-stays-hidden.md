- A model's pass stays hidden when you talk over it (#456). A model
  with nothing to add replies `[pass]` and the app hides the turn, but
  if you interrupted it while it was still writing that, the app saved
  what it had so far. The chat then showed a message reading `[pass`
  with the cut-off note, and memory kept it. Now a reply cut off while
  it could still become `[pass]` counts as a pass, and nothing is saved.
  On screen, a pass no longer flashes up as brackets while it streams,
  and a model you cut off before it wrote anything no longer leaves its
  name with nothing under it. Copy chat leaves those turns out too, so
  it no longer prints "(no text)" rows for models that stayed quiet.
