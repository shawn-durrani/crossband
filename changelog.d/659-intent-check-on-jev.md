- A new setting, `intent_check`, can have TypeSafe's Jev read each turn
  for instructions to the app instead of the utility model. Jev answers
  in about 0.35 seconds instead of 1.1, so "group mode on" or "think
  harder" reaches the reply it was said for. The utility model is still
  asked for names, and takes over if Jev fails. The default stays
  `claude`.
