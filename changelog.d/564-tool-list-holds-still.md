- The models' tool list no longer changes mid-chat (#564). It used to
  lose the Claude Code summons while a job was running, the memory tools
  when the memory service didn't answer, and an outside tool server's
  tools when it dropped, and each change made every seat write its whole
  cached prompt again. The list now follows only the chat's switches and
  what's installed. A tool that's unavailable right now answers the call
  with a reason instead: the summons says a job is already running,
  memory says nothing was read or saved, and an outside server says it's
  disconnected.
