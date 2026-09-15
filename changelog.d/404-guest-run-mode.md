- A seat can now ask Claude Code to run a command and report what it
  printed (#404). The new run mode has a shell for the project's own
  commands, such as the tests or a harness, and no way to edit, commit,
  push or open a pull request. Before, a "run this and show me" ask
  either landed in investigate mode, which has no shell, or needed
  implement mode for a read-only job. The tool now tells the seats
  that investigate cannot run anything.
