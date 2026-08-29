- Token counts and dollar amounts now format one way everywhere (#236).
  Six token formatters and four money rules had drifted apart, so a
  multi-million-token figure could still render as a five-digit "k"
  number in four places, easy to misread by a factor of a thousand.
  Message usage arrows, the export picker, the Spend page and the
  header gauge all gain the M tier, and sub-ten-cent amounts show
  graded decimals instead of flattening to $0.00 or $0.05. The
  reasoning-effort rules moved to a tested module, and a stale effort
  value now resets to Default at save instead of failing the whole
  save. Message rendering also stops rescanning the transcript per row.
