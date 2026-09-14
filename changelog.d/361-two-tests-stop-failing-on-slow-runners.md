- Two tests no longer fail on a slow CI run with nothing wrong (#361).
  The restart test for slash-command acks now uses a window a slow
  runner can't cross, and every test gets its own temp directory, so a
  guest worktree torn down late by one test can't wreck the next test's
  worktree at the same path.
