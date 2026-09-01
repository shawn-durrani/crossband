- A refused voice clip now says so (#312). The acceptance gate could
  turn away every clip a person's capture offered while the screen
  showed only "still learning" at zero seconds, with no way to tell
  silence from refusal; that is exactly how a broken speech check ran
  unnoticed for a week. Each refusal now lands in the service log
  with the failing measure, and every person's entry in the people
  and health endpoints carries how many clips were refused in the
  last week and why the last one was. Times, counts and a fixed
  reason only; never audio.
