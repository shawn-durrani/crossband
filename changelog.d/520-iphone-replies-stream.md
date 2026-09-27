- On an iPhone with iOS 17.1 or later, a spoken reply starts playing as
  its first audio arrives, as it does on a Mac (#520). The phone used to
  wait until the whole reply had been turned into speech, which left 3 to
  37 seconds of silence before a reply in the owner's traces. It now
  streams through Safari's ManagedMediaSource. If that stream fails
  before a word is heard, the reply plays whole once all of it has
  arrived, and an older iPhone plays every reply that way.
