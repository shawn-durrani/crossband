- The seat ledger now flags a reply that copies another seat's reply
  as well as a seat repeating itself (#162). A reply that matches one of
  the chat's last 12 replies from any seat is marked with the seat and
  round it copied. The match ignores case, spacing and a name label
  copied onto the front. The echo guard's catches land in the ledger
  too, naming whose reply was restated, which matters most in voice,
  where the guard can only log. Nothing changes about the reply itself:
  it still posts, speaks and reaches memory. Read the flags at
  `GET /api/models/seat_trace` or in the voice diagnostics dump.
