- A deploy now asks crossband whether it is busy before restarting it
  (#343). `GET /api/busy` answers on loopback without a session and
  reports, as fixed labels, a round still generating in any chat, a live
  voice capture, a guest visit running, a person sync pass, a benchmark,
  an import or a backup mid-copy. The fleet's deploy watcher used to
  guess from one machine-wide process search, which saw a Claude Code
  visit for any app and nothing else: it cut off rounds and imports it
  could not see, and held a restart for a visit that belonged to another
  app. Now it waits for your round or visit to finish, and no longer
  waits for unrelated work.
