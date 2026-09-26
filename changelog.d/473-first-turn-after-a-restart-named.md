- The first thing you say after the app restarts can now be named.
  The voice matcher used to load only when the first voice turn needed
  it, and that turn went unnamed while it loaded. The app restarts on
  every update, so this happened after each one. The matcher now loads
  in the background while the app starts, as long as voice
  identification is on and its model is already downloaded (#473).
