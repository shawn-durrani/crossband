- A content-free ledger of what each seat's completion did (#162). Per
  completion: the round and seat, time to first token, total time,
  chunk and character counts, the finish reason, how it ended, and a
  short hash of the reply. A reply that repeats an earlier one by the
  same seat in the same chat is marked as a repeat and logged, and a
  doubled send of the same text within ten seconds is written down by
  hash. Read it at `GET /api/models/seat_trace`, or in the voice
  diagnostics dump. Never the text itself.
