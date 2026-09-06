- Merging two remembered people now settles the corrections still
  waiting on the one merged away (#338). A move or delete waits in a
  ledger until the next sync sends it to membro, and it named the
  people involved by a local id that stopped resolving the moment one
  of them was merged away. A clip moved into that person then waited
  forever. A clip deleted out of them was dropped as nothing left to
  fix, membro's merge moved its copy into the survivor, and the next
  sync restored it here under the survivor's name: the owner deleted a
  recording and the recording came back. The merge now rewrites those
  rows as it removes the person. A move into them now targets the
  survivor, a merge they won names the survivor as winner, and a move
  or delete out of them keeps their membro address, ahead of the merge,
  so the delete lands before membro's merge moves the rest across.
  Forget has settled its rows this way since #335; one rule now serves
  both.
