- The memory summary now sits ahead of the conversation in its own
  cached block, instead of being sent at full price at the end of every
  call. It changes about once in twenty replies, so most calls read it
  back from the cache. A local model reads it once instead of on every
  turn.
