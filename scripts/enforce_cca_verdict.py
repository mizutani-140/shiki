#!/usr/bin/env python3
"""Enforce a Shiki CCA structured verdict inside GitHub Actions."""

from __future__ import annotations

import json
import os
import re
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

# A blocking item is "short-circuited" when its reason marks the item
# ``insufficient_evidence`` because another blocker is already known / the
# verdict is already determined, rather than because the item's own durable
# evidence is missing. A verdict may never leave a blocking checklist item or
# acceptance criterion unevaluated this way: ``complete`` is impossible once any
# blocker exists, so a short-circuited run produces a hollow record exactly when
# a blocker is present (observed on PR #179).
#
# Detection must not false-positive on a genuine ``insufficient_evidence``
# reason that names this item's own missing evidence (e.g. "no test covers the
# new short-circuit branch" or "the e2e check was skipped due to a missing
# binary"). So a reason is treated as short-circuited only when it is a strong
# standalone phrase, or it combines a "skip" signal with an already-blocked
# EXTERNAL-cause signal. Markers are matched against the reason after
# lowercasing, stripping apostrophes, and collapsing whitespace, hyphens, and
# underscores to single spaces.

# Phrases that on their own express an "already blocked" short-circuit.
STANDALONE_SHORT_CIRCUIT_MARKERS = (
    "already blocked",
    "already determined",
    "verdict already determined",
    "verdict already blocked",
    "verdict is already determined",
    "verdict is already blocked",
    "no need to evaluate",
)

# Phrases saying the item was not evaluated / was skipped / deferred. Every cue
# here is weak on its own (e.g. "deferred", "pending", "irrelevant", "not
# reached" all appear in genuine reasons), so a match only counts toward a
# short-circuit when it is paired with an EXTERNAL already-blocked cause below.
SKIP_MARKERS = (
    "not evaluated",
    "did not evaluate",
    "didnt evaluate",
    "not assessed",
    "not checked",
    "cannot be assessed",
    "cannot assess",
    "skipped",
    "short circuit",
    "no point",
    "moot",
    "defer",  # deferred / defers / deferring
    "not reached",
    "superseded",
    "unjudged",  # left unjudged / unjudged
    "irrelevant",
    "n/a",
    "not applicable",
    "halted",
    "pending",
)

# Phrases citing an already-existing blocker or already-decided verdict as the
# cause -- an EXTERNAL cause rather than this item's own missing evidence.
ALREADY_BLOCKED_CAUSE_MARKERS = (
    "already blocked",
    "already fail",
    "already determined",
    "already a blocker",
    "a blocker already",
    "blocker already",
    "blocker exists",
    "known blocker",
    "existing blocker",
    "verdict already",
    "verdict is already",
    "verdict was already",
    "another blocker",
    "blocked upstream",
    "failing blocker",  # the failing blocker
    "blocker above",
    "guardian gate fails",
    "cannot merge",
    "first blocking failure",
)

# A checklist reference like "CCA-08" that names a DIFFERENT item than the one
# being judged is itself an EXTERNAL already-blocked cause: the most natural way
# a judge phrases a cross-item short-circuit is "deferred pending resolution of
# CCA-08" or "blocked by CCA-08, so this was left unjudged" -- a bare reference
# to the item that already failed, with no failure word attached. A reference to
# the item's OWN id is self-reference (the judge naming the item it is judging)
# and must NOT count as an external cause. After normalization "CCA-08"
# collapses to "cca 08", so references are matched against the normalized reason
# and compared to the normalized own id.
CCA_REFERENCE_RE = re.compile(r"cca \d+\w*")


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


def _normalize_reason(reason: str) -> str:
    lowered = reason.lower().replace("'", "").replace("’", "")
    return re.sub(r"[\s_-]+", " ", lowered).strip()


def _references_external_item(normalized: str, item_id: Any) -> bool:
    """True when the reason names a checklist item OTHER than the one being judged.

    A reference to another item's id (e.g. ``CCA-08`` on item ``CCA-05``) blames
    an external blocker. A reference to the item's own id is self-reference and
    is not treated as an external cause.
    """
    own = _normalize_reason(item_id) if isinstance(item_id, str) else ""
    return any(ref != own for ref in CCA_REFERENCE_RE.findall(normalized))


def _is_already_blocked_reason(reason: Any, item_id: Any = None) -> bool:
    """True when ``reason`` blames an existing blocker instead of naming this item's own missing evidence."""
    if not isinstance(reason, str):
        return False
    normalized = _normalize_reason(reason)
    if any(marker in normalized for marker in STANDALONE_SHORT_CIRCUIT_MARKERS):
        return True
    skipped = any(marker in normalized for marker in SKIP_MARKERS)
    blocked_cause = any(
        marker in normalized for marker in ALREADY_BLOCKED_CAUSE_MARKERS
    ) or _references_external_item(normalized, item_id)
    return skipped and blocked_cause


def short_circuited_evaluations(verdict: dict[str, Any]) -> list[str]:
    """Blocking items left ``insufficient_evidence`` with an 'already blocked' reason.

    Every blocking checklist item and every acceptance criterion must be judged
    on its own durable evidence, even when another blocking item already fails.
    An item marked ``insufficient_evidence`` because the verdict is already
    blocked -- rather than because its own evidence is missing -- is a
    short-circuited evaluation and is reported here so the enforcer can reject
    the verdict.
    """
    offenders: list[str] = []
    for item in verdict.get("checklist") or []:
        if not isinstance(item, dict):
            continue
        if item.get("blocking") is not True:
            continue
        status = str(item.get("status") or "").strip().lower()
        if status != "insufficient_evidence":
            continue
        if _is_already_blocked_reason(item.get("reason"), item.get("id")):
            offenders.append(f"checklist item {item.get('id') or '<unknown>'}")
    for item in verdict.get("acceptance") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status != "insufficient_evidence":
            continue
        if _is_already_blocked_reason(item.get("reason"), item.get("criterion")):
            criterion = item.get("criterion") or "<unknown>"
            offenders.append(f"acceptance criterion {criterion!r}")
    return offenders


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

    short_circuited = short_circuited_evaluations(verdict)
    if short_circuited:
        raise SchemaValidationError(
            "blocking evaluation short-circuited: "
            + ", ".join(short_circuited)
            + "; every blocking checklist item and acceptance criterion must be evaluated "
            "on its own evidence even when another blocker is already known, not marked "
            "insufficient_evidence because the verdict is already blocked"
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
