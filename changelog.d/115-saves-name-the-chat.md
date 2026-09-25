- A fact a model saves while a guest is in the room now stays in that
  chat once you approve it (membro#115, contract 1.7). Every direct
  `save_memory` carries the chat it was made in, the same pair ingest
  and recall use, and membro binds a guest-present save to it. A save
  made with only you in the room is recalled in every chat as before.
  An older membro ignores the field, so its guest-present saves stay
  global until it's updated.
