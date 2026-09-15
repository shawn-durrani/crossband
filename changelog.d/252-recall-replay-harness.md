- The ambient memory recall can now be measured on your own install
  (#252). A new offline harness, eval_recall, replays your user turns
  through the same recall a round runs, checks each fact against the
  standing summary, and reports a floor sweep, a per-rank table and the
  latency, so the count and the floor can be tuned on numbers. A keyless
  mock run checks the harness; a real run needs membro up. Nothing in a
  live round changes.
