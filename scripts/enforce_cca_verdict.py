#!/usr/bin/env python3
"""Enforce a Shiki CCA structured verdict inside GitHub Actions."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


VALID_VERDICTS = {
    "complete",
    "repair_required",
    "blocked",
    "needs_guardian",
    "insufficient_evidence",
}


def fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def load_verdict() -> dict[str, Any]:
    raw = os.environ.get("STRUCTURED_OUTPUT", "").strip()
    if raw:
        return json.loads(raw)

    path = Path(os.environ.get("CCA_VERDICT_FILE", ".shiki/gha/cca-verdict.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    try:
        verdict = load_verdict()
    except Exception as error:  # noqa: BLE001 - this is a CLI boundary.
        return fail(f"could not load CCA verdict: {error}")

    output_path = Path(os.environ.get("CCA_VERDICT_FILE", ".shiki/gha/cca-verdict.json"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    status = verdict.get("verdict")
    if status not in VALID_VERDICTS:
        return fail(f"invalid CCA verdict: {status!r}")

    if status == "complete":
        print("CCA verdict complete; MergeGate may evaluate readiness")
        return 0

    repair_packet = verdict.get("repair_packet")
    if status == "repair_required" and not repair_packet:
        return fail("CCA verdict repair_required must include a repair_packet")

    print(f"CCA verdict is {status}; MergeGate is blocked")
    print(json.dumps(verdict, indent=2, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
