- Three shipping paths gained their first tests (#242). The attachment
  projection is pinned per kind on both provider sides, including the
  truncation cap and the framing both providers must share. The
  continue endpoint's round choreography is driven end to end,
  including the shared one-round lock with send. The frontend's fetch
  wrapper has a suite pinning that failures reject, a 401 raises the
  lock screen exactly once, and path segments stay encoded.
