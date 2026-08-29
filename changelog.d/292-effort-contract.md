- The reasoning-effort choices the seat editor offers, and the Claude
  models it greys out, are now checked against the backend's own rules
  by the cross-language contract test (#292). If the two sides drift,
  CI goes red instead of the editor quietly offering stale choices.
