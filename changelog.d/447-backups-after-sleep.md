- Backups no longer stall while the Mac sleeps. The six hours between
  automatic snapshots used to count only time the Mac was awake, so a
  laptop that slept most of the day could go days without a new restore
  point. The timer now checks every five minutes and goes by the clock,
  so a snapshot that fell due during sleep is taken within five minutes
  of the Mac waking. A copy identical to the newest snapshot is still
  thrown away. Setting `backup_interval_hours` to `0` now turns the timer
  off, where it used to copy the database over and over (#447).
