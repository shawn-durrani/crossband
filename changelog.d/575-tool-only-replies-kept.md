- A model that uses a tool and then writes nothing, or passes, keeps the
  record of what its tools did (#575). The app saved no message for such
  a reply, so the tool activity you saw during the round was gone after
  a reload, and the other models never saw the results. It's now saved
  as a message with no words and its tool calls, and so is a call that
  fails or that you talk over after its tools ran.
