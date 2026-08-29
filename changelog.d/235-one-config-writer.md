- config.local.json now has exactly one writer (#235). The pricing API
  kept a private copy of the atomic write that config.py already owns,
  and the shared function's comment claimed a consolidation that had
  not happened. The copy is deleted, both pricing saves go through the
  one path, and the docstring now tells the truth.
