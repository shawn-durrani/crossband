"""Offline evaluation harness for the memory-grounding critic.

This package does NOT touch the live response path (backend/engine.py). It
is a standalone harness: synthetic fixtures + a critic caller (reusing
backend.llm_util.utility_complete's routing) + scoring, run offline to decide
whether a live output-side critic clears the bar worth building for. See
eval_critic/README.md for the fixture schema, the evidence-precedence policy,
and how to point the harness at a private fixtures directory kept outside the
repository.
"""
