- The pass and echo guards inside a round are now one pure decision
  (#241). Four interacting flags across a retry loop were discoverable
  only by reading all of it; the judgement table is extracted and pinned
  by tests, the round's tail moved to its own function, and two dead
  writes that a rebuilt dict discarded are gone. The rest of the round
  recipe deliberately stays one documented function.
