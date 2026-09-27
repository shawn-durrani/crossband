- The transcriber and the voice checks hear cleaner audio (#505). The
  browser brings the mic down to the 16 kHz they read, and it used to do
  that by keeping one sample in three, which folded hiss, taps and other
  high sounds into the speech range as sounds nobody made. It now filters
  them out first, for the live stream and for the backup copy of a turn.
