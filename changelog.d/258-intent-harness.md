- The spoken instruction gap can now be measured (#258). A new offline
  harness, eval_intent, holds made-up turns graded by hand and reports
  how many instruction wordings today's phrase lists never send to a
  model, and how one merged model call scores against today's four
  gated prompts on the same turns. A keyless mock run checks the
  harness and the dropped-turn count is real in it; the comparison
  needs a key. Nothing the app hears changes yet.
