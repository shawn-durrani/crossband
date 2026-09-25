- Crossband now pins the Anthropic Python SDK to its 1.x line, at
  1.8.0 or newer. The requirement carried no version, so a fresh install
  and CI already got 1.8.0 while an older install kept the 0.x release
  it first installed, and the two could quietly differ. Nothing in
  crossband's own code needed changing for 1.x. Chat replies, streaming,
  the model list and the small utility calls work as before. An existing
  install moves to 1.8.0 on its next start, because `start.sh`
  reinstalls the Python packages whenever `requirements.txt` changes.
