- A new setting, `turn_end_shadow`, runs the end-of-turn trial. At every
  pause in a spoken turn, Jev reads your words so far and Smart Turn, a
  small model on this computer, hears your audio, and each guesses
  whether you'd finished. The guesses are kept beside what you did next,
  and nothing acts on them. `scripts/turn_end_shadow_report.py` sets them
  side by side. It needs onnxruntime, now in requirements.txt.
