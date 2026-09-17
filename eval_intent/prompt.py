"""Moved to `backend/intent.py` (#412): the merged prompt is now the app's
own, not the harness's, so the live scan and this harness can never drift
apart. Re-exported here only because this sandbox cannot delete a file - new
code should import `backend.intent` directly, which is what
`eval_intent/runner.py` does."""

from backend.intent import build_merged_prompt  # noqa: F401
