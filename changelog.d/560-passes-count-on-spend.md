- The Spend page now counts every model call you pay for (#560). A
  model's `[pass]`, a first try the app turned down and asked for again,
  and an empty reply were all paid calls that left no message, and the
  app only recorded cost on messages, so about a quarter of Claude seat
  spend never showed. Each is now recorded without a message, so a pass
  stays invisible in the chat while its cost counts in the Spend page,
  the chat's running cost and the prompt cache numbers. The detail view
  says how much of the window went on them.
