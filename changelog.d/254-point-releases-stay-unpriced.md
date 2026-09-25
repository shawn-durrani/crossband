- A point release of a priced model no longer borrows the older
  model's price (#254). The price card read a one-digit version as a date
  stamp, so `claude-opus-5-5` and `claude-fable-5-1` recorded Opus 5
  and Fable 5 rates they may not have. A stamp now needs four digits or
  more, so a seat on a point release records unknown cost until you
  price it on the Models page, and a dated reissue such as
  `claude-haiku-4-5-20251001` still prices as before.
