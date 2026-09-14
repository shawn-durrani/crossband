- The transcript-shape experiment can now be run (#212). A new offline
  harness, eval_attribution, replays synthetic group chats through three
  projection shapes (today's, own turns labelled, and the member-envelope
  shape) and asks each seat who said what, including about itself. It uses
  the real seat prompt, prices each call, and lists every miss with the
  reply text. A keyless mock run checks the harness; a real run needs the
  provider keys. Nothing in a live round changes.
