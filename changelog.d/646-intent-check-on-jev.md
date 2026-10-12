- The spoken intent check can now measure TypeSafe's Jev beside the
  small Claude model, with `--strategy jev`. Jev answers the yes-or-no
  and multiple-choice parts of every turn at once, and Claude is asked
  only for names. Nothing in the app changes yet. The Jev client the
  interruption judge uses now lives in its own module, `backend/jev.py`.
