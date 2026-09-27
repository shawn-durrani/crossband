- A clip the app drops from a voice now leaves membro too (memory
  contract 1.8). Membro kept every clip it was ever sent, so it held far
  more of each voice than the app uses. When rotation drops a clip, a
  settled bank replaces one, or the hygiene guard sets one aside, the
  next sync deletes membro's copy through the same ledger as your own
  deletes, with the reason, so a rebuild can't bring it back. A set-aside
  clip goes back up if a later check reinstates it. Each sync also sends
  membro the list of clips every voice keeps, so its People page can
  count the rest and delete them when you press its button. A 1.7 membro
  gets no list and still takes the deletes. Deploy after membro.
