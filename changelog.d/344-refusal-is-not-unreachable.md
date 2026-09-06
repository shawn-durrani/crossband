- A membro that refuses Crossband's token is no longer logged as
  "membro unreachable" (#344). The person sync said that at INFO, a
  level the default install discards, so a half-done token rotation
  (membro's `.env` updated, Crossband's not) left search, sync and job
  polling dead with nothing in the log. Every refusal now logs at
  WARNING, once until the outcome changes, names the call membro
  refused and says the likely cause: `MEMORY_AUTH_TOKEN` in Crossband's
  `.env` no longer matches membro's copy. Membro being down keeps its
  wording and moves to WARNING too.
