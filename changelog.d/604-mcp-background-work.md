- Long work an MCP app runs, such as a design app building a cabinet, now
  carries on in the background while you keep talking. The app watches
  it, and a strip over the composer shows where it's got to, like
  "Dovetail: adding the drawer runners · step 48 · 5 min". Ask the AIs
  how it's going and they answer straight away, and they can pass a
  change on to it while it runs. A question it's waiting on comes back
  to the room as soon as the chat is between turns, a short progress
  line comes at a pause no more than every couple of minutes, and its
  result is relayed when it's done. Talking over an AI or sending a new
  message never stops it. The new `mcp_progress_every_s` setting sets
  how often the progress line may come, and `0` keeps it quiet.
