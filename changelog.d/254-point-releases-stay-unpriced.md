- A point release of a priced model no longer borrows the older
  model's price (#254). The price card read a one-digit version as a date
  stamp, so a new point release recorded the older model's rates, which it
  may not share. A stamp now needs four digits or more, so a point release
  with no row of its own records unknown cost until you price it on the
  Models page, and a dated reissue such as `claude-haiku-4-5-20251001`
  still prices as before.
