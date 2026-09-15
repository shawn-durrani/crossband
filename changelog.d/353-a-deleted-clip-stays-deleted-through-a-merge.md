- A clip you deleted stays deleted through a merge (#353). Moving a
  clip into a person, deleting it out of them and then merging them away
  before a sync used to leave membro holding the clip, and the restore
  step handed it back. The ledger now follows each clip through its
  rows, so the delete looks where the clip actually ended up.
