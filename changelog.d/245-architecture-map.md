- ARCHITECTURE.md's shape block now maps the whole backend: the routers,
  the round buffer, the Membro bridge and the four voice-identity modules
  each have a line. The docs index lists the eval harness READMEs, CI
  enforces that, and the documentation guards moved from the plist suite
  into tests/test_doc_style.py where a reader would look for them.
