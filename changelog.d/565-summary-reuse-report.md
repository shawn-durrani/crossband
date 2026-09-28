- A new report says how often the memory summary repeats between
  Claude seat calls, and what caching it in a block of its own would
  save at each model's rate card (#565). Each call now records a
  fingerprint and the length of the summary it sent, never its text.
  Run `.venv/bin/python scripts/summary_reuse_report.py` for the last
  seven days. It reads the database and changes nothing.
