- A model that repeats its chat's secret context marker no longer shows
  it on screen, or says it aloud in a voice chat, while its reply
  streams (#574). The saved reply was already clean, and now the live
  reply and the tool activity beside it are too. Only the few
  characters that could be the start of the marker wait for the next
  piece of the reply, so nothing else is slowed down.
