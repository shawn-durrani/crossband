- The live-microphone list (the every-surface mic banner, and the new
  diagnostics dump) is read from a snapshot now, so reading it at the
  exact instant a capture session closes can no longer fail the request.
