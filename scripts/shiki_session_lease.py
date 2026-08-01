#!/usr/bin/env python3
"""Public import surface for the OS session-lease primitives.

``shiki_runtime.dispatch_runner_task`` writes ``running`` to the task file before
a synchronous adapter call and the terminal status only after it returns, so a
session that dies between those writes (the command-timeout strand, reproduced
2026-07-29..31) leaves the task at ``running`` with no live process and
``shiki_loop`` reports ``wait_runner`` forever. The fix is an exclusive
``fcntl.flock`` held for the session's lifetime — the kernel releases it on
process death, so a successful non-blocking acquire is exact proof no holder is
alive — plus a loop decision that consults it.

The primitives are DEFINED in ``shiki_runtime`` — a module the installer stages
into every Shiki repo — so a dispatched session in the coordinator AND in target
repos carries the lease. (A dedicated top-level module would have to be added to
the installer's ``TEMPLATE_PATHS`` to reach target repos; keeping the definitions
in the already-staged ``shiki_runtime`` avoids leaving target ``shiki init`` /
dispatch broken.) This module re-exports that stable API so tests, tooling, and
the loop can ``import shiki_session_lease`` without depending on placement:

* ``hold_session_lease(target, task_id)`` — a context manager the dispatcher
  wraps its session in; it takes and holds the exclusive lease and releases it on
  exit and on process death alike.
* ``session_lease_state(target, task_id)`` — a read-only probe returning
  ``held`` / ``free`` / ``foreign_host`` / ``absent`` that never leaves the lease
  acquired.
"""

from __future__ import annotations

from shiki_runtime import (
    LEASE_ABSENT,
    LEASE_FOREIGN_HOST,
    LEASE_FREE,
    LEASE_HELD,
    SessionLeaseHeld,
    hold_session_lease,
    session_lease_path,
    session_lease_state,
)

# Aliases matching this module's own vocabulary (``lease_path`` reads naturally
# from ``shiki_session_lease.lease_path``); ``session_lease_path`` is re-exported
# too for callers that prefer the fully-qualified name.
lease_path = session_lease_path

# The four probe states, re-exported under short names.
HELD = LEASE_HELD
FREE = LEASE_FREE
FOREIGN_HOST = LEASE_FOREIGN_HOST
ABSENT = LEASE_ABSENT

__all__ = [
    "SessionLeaseHeld",
    "hold_session_lease",
    "session_lease_state",
    "session_lease_path",
    "lease_path",
    "HELD",
    "FREE",
    "FOREIGN_HOST",
    "ABSENT",
]
