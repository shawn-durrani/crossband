- Every voice label write now builds its payload in one place (#237).
  Six copies of the label-one-turn sequence had already let two paths
  drift, which is how the owner path lost its tap-to-correct audio and
  the arm path lost its mismatch cross-check. This change is the
  consolidation only; the two behaviour repairs are their own decisions
  on the issue.
