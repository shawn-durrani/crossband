- Voice stalls can now be reported from the phone, evidence and all
  (#304). The app keeps a short in-memory log of what a voice session
  did - states, timings, round events and any red error text - never
  what anyone said. "Save voice diagnostics" in the voice settings
  writes that log to one server-side file, beside the live capture
  sessions, the chat's recent identity decisions, the parked-label
  outcomes and the latency summary. The health endpoint now also shows
  the last few identity decisions per chat (with the turn each decided)
  and whether a parked label was claimed or expired unclaimed, so a
  stalled turn's evidence survives the next turn overwriting the live
  readouts.
