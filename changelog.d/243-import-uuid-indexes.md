- Re-importing a provider export no longer scans every chat row per
  conversation (#243). The import idempotency lookups now ride partial
  indexes, measured 76 times faster at five thousand conversations.
  Rows that never came from an export cost nothing.
