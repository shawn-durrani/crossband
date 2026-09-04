- Forgetting someone now settles the corrections still waiting on them
  (#335). A move, delete or merge waits in a ledger until the next sync
  sends it to membro, and it named the people involved by a local id
  that stopped resolving the moment one of them was forgotten. The row
  then waited forever. Worse, a merge whose winner was forgotten left
  the other record living in membro, and the next sync rebuilt that
  person here, audio and all: the owner forgot a human and the human
  came back. Forget now rewrites those rows as it removes the person.
  The other record in a merge they won is forgotten too, a clip moved
  into them is deleted at its source, and a move or delete out of them
  keeps their membro address so it can still land. The same settling
  runs when the forget came from membro, and the sync applies membro's
  forget marks before it rebuilds anyone, so a settled merge's loser
  cannot be pulled back in between.
