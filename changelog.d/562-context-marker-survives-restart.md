- A restart no longer makes every seat write its cached prompt again
  (#562). The secret marker that vouches for the app's own context block
  used to be made fresh at each start, and the cached part of every
  prompt names it. Each chat's marker now comes from a key the app keeps
  in `data/context_marker.key`, readable only by your account, so it
  stays the same across restarts. A model that repeats its marker has it
  taken out before the reply is saved or a tool runs.
