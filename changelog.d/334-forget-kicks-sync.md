- Forget reaches memory the moment you press it (#334). Forgetting a
  voice on the Voices page deletes the audio here at once, but the copy
  in membro used to wait for the next sync pass: after a round, at most
  every two minutes, or at startup. For a round or two the explainer
  said the audio was gone in both apps while it was gone here only, and
  nothing on screen said the rest was still on its way. Forget now
  starts that pass itself, in the background, and answers as quickly as
  before. Membro down, or no token, is the same logged no-op it always
  was, and the forget stays in the ledger for the next pass. Moves,
  deletes and merges still travel with the round-end pass; they correct
  who said what, they do not remove a person.
