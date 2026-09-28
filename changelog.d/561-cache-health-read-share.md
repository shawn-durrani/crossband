- Prompt cache health on the Spend page now judges each model on the
  share of all its input read back from the cache (#561). It used to
  compare cache reads with cache writes only, which left out the input
  sent at full price, so a seat reading only 62% of its input from the
  cache showed as healthy. Now 80% or more is healthy, 50 to 80% is one
  to watch, and under half is poor, and GPT seats get a real verdict
  too. Each row shows what was read, written and sent at full price.
