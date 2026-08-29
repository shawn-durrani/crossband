- Three chat routes (incremental messages, the guest-job snapshot, and
  voice-turn discard) no longer run their database reads on the event
  loop (#243). They run in the request threadpool, so a slow disk read
  cannot stutter live voice, and the incremental route runs on every
  new message.
