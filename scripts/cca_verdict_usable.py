#!/usr/bin/env python3
"""Classify a CCA structured output as usable or one of three unusable classes.

PR #233 reproduced the defect this guards. The CCA step's structured output was
consumed exactly once, with no classification between generation and
enforcement, so an *unusable generation* -- an empty output, a non-JSON blob, or
a schema-valid-but-empty "degenerate" verdict that judged nothing -- was
indistinguishable from a real negative verdict. The only recovery was a full
manual ``gh run rerun`` OUTSIDE the run, and the cheaper-looking
``gh run rerun --failed`` reused the previous job's artifact and reproduced the
identical stale degenerate verdict, so the recovery that looks cheaper is the
one that cannot work. The loop had no way to express the one that does.

``verdict_usability`` is the pure classifier the workflow consults BETWEEN
``Run CCA`` and ``Enforce CCA verdict``. Only ``absent``, ``unparseable`` and
``degenerate`` are regenerated, and exactly once. Every real answer -- a
well-formed ``complete``, ``repair_required``, ``needs_guardian``, ``blocked``
or ``insufficient_evidence`` -- is ``usable`` and is NEVER regenerated:
retrying a real negative verdict until it changes would be judge-shopping, and
the classifier exists precisely to make that impossible to do by accident.

The classifier is pure and does no I/O; the ``__main__`` CLI below reads the
structured output from the ``STRUCTURED_OUTPUT`` environment variable (the same
handoff ``enforce_cca_verdict.py`` uses) or from stdin, and prints
``{"classification": ..., "reason": ...}`` for the workflow to consume.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

USABLE = "usable"
ABSENT = "absent"
UNPARSEABLE = "unparseable"
DEGENERATE = "degenerate"

CLASSIFICATIONS = (USABLE, ABSENT, UNPARSEABLE, DEGENERATE)

# ``checklist`` and ``acceptance`` each stand for a dimension the CCA must judge;
# a PRESENT-but-empty list for either means the judge evaluated nothing for that
# dimension -- the exact PR #233 hollow verdict. This deliberately mirrors
# ``enforce_cca_verdict.DEGENERATE_JUDGMENT_ARRAYS``: the enforcer rejects the
# same shape, and this classifier lets the workflow regenerate it once BEFORE it
# ever reaches the enforcer. A MISSING array is a different fault -- a missing
# required property, left to schema validation at enforcement -- so it is NOT
# degenerate here.
JUDGMENT_ARRAYS = ("checklist", "acceptance")


def verdict_usability(structured_output: Any) -> tuple[str, str]:
    """Classify a CCA structured output. Pure: no I/O, no mutation of the input.

    Accepts the raw structured-output string the CCA step emits (the workflow's
    reality) or an already-parsed object (convenient for callers and tests).
    Returns ``(classification, reason)`` where ``classification`` is one of
    ``usable``, ``absent``, ``unparseable`` or ``degenerate`` and ``reason`` is a
    human-readable explanation an operator can read off the job log.
    """
    # ABSENT: nothing was emitted. ``None`` (no output binding at all) or a
    # string that is empty or whitespace-only.
    if structured_output is None:
        return (ABSENT, "structured output is absent: no value was produced")
    if isinstance(structured_output, str):
        if structured_output.strip() == "":
            return (ABSENT, "structured output is absent: empty or whitespace-only")
        try:
            parsed = json.loads(structured_output)
        except ValueError as error:
            return (UNPARSEABLE, f"structured output is not valid JSON: {error}")
    else:
        # Already-parsed value handed in directly.
        parsed = structured_output

    # UNPARSEABLE (shape): valid JSON but not a verdict object. A verdict is a
    # JSON object; an array, number, string or null can never be enforced as one.
    if not isinstance(parsed, dict):
        return (
            UNPARSEABLE,
            f"structured output is not a JSON object: parsed a JSON {type(parsed).__name__}",
        )

    # DEGENERATE: schema-valid in shape but judging nothing -- a present-but-empty
    # checklist or acceptance list. Each stands for a dimension the CCA must judge.
    empty = [
        name
        for name in JUDGMENT_ARRAYS
        if isinstance(parsed.get(name), list) and not parsed.get(name)
    ]
    if empty:
        joined = " and ".join(empty)
        plural = "list is empty" if len(empty) == 1 else "lists are empty"
        return (
            DEGENERATE,
            f"verdict judged nothing: the {joined} {plural}",
        )

    # USABLE: a real answer of ANY verdict value. A well-formed repair_required,
    # needs_guardian, blocked or insufficient_evidence is a real negative verdict
    # and must never be regenerated -- that would be judge-shopping.
    verdict = parsed.get("verdict")
    return (USABLE, f"structured output is a usable verdict: {verdict!r}")


def _read_structured_output() -> Any:
    """Read the structured output for the CLI.

    Prefer ``STRUCTURED_OUTPUT`` (the same env handoff the CCA workflow uses for
    ``enforce_cca_verdict.py``); when it is unset entirely, fall back to stdin.
    An env var that is present but empty is a real signal (the CCA step produced
    no output) and is passed through unchanged so it classifies as ``absent``.
    """
    if "STRUCTURED_OUTPUT" in os.environ:
        return os.environ["STRUCTURED_OUTPUT"]
    return sys.stdin.read()


def main() -> int:
    classification, reason = verdict_usability(_read_structured_output())
    # Classification is informational; the workflow decides whether to retry and
    # the enforce step is what fails the job. Always exit 0.
    print(json.dumps({"classification": classification, "reason": reason}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
