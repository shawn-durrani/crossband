"""Thin wrapper: `.venv/bin/python scripts/run_critic_eval.py [args]` runs the
offline critic eval harness (see eval_critic/README.md). Equivalent to
`.venv/bin/python -m eval_critic.runner [args]` — this file exists only so
the harness has an entrypoint alongside the project's other scripts."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eval_critic.runner import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
