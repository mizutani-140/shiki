#!/usr/bin/env python3
"""Enforce a Shiki CCA structured verdict inside GitHub Actions."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from shiki_schema import SchemaValidationError, validate_instance


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


def load_schema(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object schema")
    return data


def blocking_checklist_failures(verdict: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for item in verdict.get("checklist") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if item.get("blocking") is True and status in {"fail", "insufficient_evidence"}:
            failures.append(str(item.get("id") or "<unknown>"))
    return failures


def validate_verdict(verdict: dict[str, Any]) -> None:
    schema = load_schema(Path(".shiki/schemas/cca-verdict.schema.json"))
    validate_instance(verdict, schema)

    status = verdict.get("verdict")
    if status not in VALID_VERDICTS:
        raise SchemaValidationError(f"$.verdict: invalid CCA verdict {status!r}")

    repair_packet = verdict.get("repair_packet")
    if status == "repair_required" and not isinstance(repair_packet, dict):
        raise SchemaValidationError("$.repair_packet: repair_required verdict must include a non-null object")
    if repair_packet is not None:
        repair_schema = load_schema(Path(".shiki/schemas/repair-packet.schema.json"))
        validate_instance(repair_packet, repair_schema, path="$.repair_packet")

    failures = blocking_checklist_failures(verdict)
    if status == "complete" and failures:
        raise SchemaValidationError(
            "complete verdict contains blocking failed checklist items: " + ", ".join(failures)
        )


def main() -> int:
    try:
        verdict = load_verdict()
        if not isinstance(verdict, dict):
            return fail("CCA verdict must be a JSON object")
        validate_verdict(verdict)
    except Exception as error:  # noqa: BLE001 - this is a CLI boundary.
        return fail(f"invalid CCA verdict: {error}")

    output_path = Path(os.environ.get("CCA_VERDICT_FILE", ".shiki/gha/cca-verdict.json"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    status = verdict.get("verdict")
    if status == "complete":
        print("CCA verdict complete; MergeGate may evaluate readiness")
        return 0

    print(f"CCA verdict is {status}; MergeGate is blocked")
    print(json.dumps(verdict, indent=2, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
