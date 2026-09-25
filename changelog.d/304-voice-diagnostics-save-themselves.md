- When voice gets stuck, the app now saves the voice diagnostics by itself
  (#304). If a turn you finished still hasn't reached the models 30
  seconds later, or a round goes quiet and never ends, the app saves the
  same file the "save voice diagnostics" button does and shows one quiet
  line saying where it went. Like the button, it keeps what the app was
  doing and never what anyone said. It saves at most once every ten
  minutes and three times per page load, so a patchy connection can't
  fill the folder.
