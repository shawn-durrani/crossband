"""The loop/threadpool split for chat routes, pinned (#243).

Three routes did blocking SQLite as `async def`, so every call ran on the
event loop the voice relays share, and the incremental-messages route runs
on every new_message event. They are plain functions now, threadpooled by
FastAPI. The split's other half matters just as much: three routes are
async ON PURPOSE (engine.spawn and the events-bus notify need the running
loop), and a cleanup pass must not strip them.
"""
import inspect

from backend.routers import benchmark, chats


def test_blocking_sqlite_routes_are_plain_functions():
    """Sync means threadpooled: a slow disk read cannot stutter live voice."""
    for fn in (chats.get_chat_messages_after, chats.get_chat_guest_jobs,
               chats.discard_turn):
        assert not inspect.iscoroutinefunction(fn), fn.__name__


def test_loop_bound_routes_stay_async():
    """The inverse constraint, encoded: these need the running loop
    (engine.spawn, or db.insert_message's notify), and their docstrings say
    so. Converting one breaks background work quietly."""
    for fn in (chats.post_notice, chats.distill, chats.continue_round,
               benchmark.start):
        assert inspect.iscoroutinefunction(fn), fn.__name__
