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

# The head SHA in the structured output is model-supplied and has been mistyped
# (PR #195 wrote a value four characters short of the real head SHA), which then
# fails the MergeGate head_sha checks even though the run head and the PR head
# are byte-identical. When the workflow provides an authoritative head SHA via
# ``SHIKI_HEAD_SHA``, that value -- not the model's -- is the source of truth.
HEAD_SHA_ENV = "SHIKI_HEAD_SHA"

# The model's reported head SHA is preserved here when it disagrees with the
# authoritative value, so the disagreement is auditable in the uploaded evidence
# rather than silently erased.
REPORTED_HEAD_SHA_KEY = "head_sha_reported"

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

# A rejected verdict is NOT written to the operative ``cca-verdict.json`` (every
# consumer, and the loop's resolver, reads that path as the CCA's authority). It
# is instead recorded on a SIBLING path derived from ``CCA_VERDICT_FILE`` so a
# refusal leaves a readable record of WHY, distinguishable from a crash, without
# ever laundering the refused verdict into an authority. The loop reads this
# record where it resolves the verdict (``shiki_loop.CCA_REFUSAL_BASENAME`` is
# kept byte-identical to ``refusal_record_path(cca-verdict.json).name``).
REFUSAL_RECORD_INFIX = "refusal"
REFUSAL_RECORD_KIND = "cca-verdict-refusal"

# A refusal is TRANSIENT when its cause is a blocking checklist item or acceptance
# criterion the judge left ``insufficient_evidence`` because that item's evidence
# was NOT YET AVAILABLE at judge time -- the CCA is itself one of the required
# checks that fire concurrently, so a sibling check still in flight cannot be
# proven yet (observed on PR #291/#292; a plain re-run after the checks settle
# then reaches ``complete``). It is NOT transient when the evidence is genuinely
# MISSING, or the item durably ``fail``ed, or the fault is structural (a degenerate
# array, a schema violation, a short-circuited evaluation). Classification is by
# the item's ``reason`` TEXT using the SAME normalization ``_is_already_blocked_
# reason`` uses; it adds no status value and changes no schema. Markers are matched
# against the reason after lowercasing, stripping apostrophes, and collapsing
# whitespace, hyphens, and underscores to single spaces.
TRANSIENT_NOT_YET_MARKERS = (
    "not yet available",
    "not yet complete",
    "not yet completed",
    "not yet finished",
    "not yet run",
    "not yet reported",
    "not yet posted",
    "not yet judged",
    "in progress",  # in-progress / in_progress collapse to this
    "in flight",
    "still running",
    "still in progress",
    "still pending",
    "currently running",
    "concurrent",  # concurrent / concurrently
    "has not completed",
    "have not completed",
    "not completed yet",
    "yet to complete",
    "yet to run",
    "queued",
    "awaiting completion",
    "will be available",
    "once the check",
    "when the check completes",
    "check is running",
    "checks are running",
    "checks still",
    "completion race",  # the "CCA completion race" (same-head concurrent checks)
)


def fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def refusal_record_path(verdict_file: Path) -> Path:
    """The sibling path a refused verdict is recorded on, derived from the verdict
    file. ``cca-verdict.json`` -> ``cca-verdict.refusal.json``: always distinct
    from ``verdict_file`` (a different stem), so the operative authority path is
    never overwritten with a refusal."""
    return verdict_file.with_name(
        f"{verdict_file.stem}.{REFUSAL_RECORD_INFIX}{verdict_file.suffix}"
    )


def load_verdict() -> dict[str, Any]:
    raw = os.environ.get("STRUCTURED_OUTPUT", "").strip()
    if raw:
        return json.loads(raw)

    path = Path(os.environ.get("CCA_VERDICT_FILE", ".shiki/gha/cca-verdict.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def inject_authoritative_head_sha(verdict: dict[str, Any]) -> dict[str, Any]:
    """Overwrite ``verdict['head_sha']`` with the workflow-supplied head SHA.

    When ``SHIKI_HEAD_SHA`` is set and non-empty, treat it as the authoritative
    head SHA: set ``verdict['head_sha']`` to it (before validation and before the
    verdict file is written). If the model reported a different value, preserve
    the model's value under ``head_sha_reported`` and warn, naming both, so the
    disagreement is auditable rather than silently erased.

    When ``SHIKI_HEAD_SHA`` is unset or empty, the verdict is returned untouched
    so local and offline invocations behave exactly as before.
    """
    authoritative = os.environ.get(HEAD_SHA_ENV, "").strip()
    if not authoritative:
        return verdict

    reported = verdict.get("head_sha")
    if reported == authoritative:
        return verdict

    if isinstance(reported, str) and reported:
        verdict[REPORTED_HEAD_SHA_KEY] = reported
        print(
            f"WARNING: CCA head_sha reported by the model ({reported!r}) does not "
            f"match the authoritative {HEAD_SHA_ENV} ({authoritative!r}); using the "
            f"authoritative value and preserving the reported value under "
            f"{REPORTED_HEAD_SHA_KEY!r}.",
            file=sys.stderr,
        )
    verdict["head_sha"] = authoritative
    return verdict


def load_schema(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object schema")
    return data


# ``checklist`` and ``acceptance`` each stand for a dimension the CCA must
# judge. A verdict that carries an EMPTY ``checklist`` or an EMPTY ``acceptance``
# list judged nothing for that dimension: it is a hollow record that "judged
# nothing" (observed on PR #233, where it was schema-VALID and slipped through to
# a downstream MergeGate rule). ``minItems: 1`` on both arrays in
# ``cca-verdict.schema.json`` now rejects it, but the schema's message is a
# generic "array length must be >= 1" that names neither the array nor the
# judgment it stands for. This explicit check runs FIRST -- before schema
# validation -- so the operator reading a red X sees exactly which dimension the
# judge left empty.
#
# Only a PRESENT-but-empty list is degenerate here. A MISSING array is a
# different fault -- the required property is absent -- and is deliberately left
# to schema validation so its "missing required property" message is preserved.
DEGENERATE_JUDGMENT_ARRAYS = ("checklist", "acceptance")


def degenerate_judgment_arrays(verdict: dict[str, Any]) -> list[str]:
    """Names of judgment arrays that are present but empty.

    ``checklist`` and ``acceptance`` each stand for a dimension the CCA must
    judge; an empty list for either means the judge evaluated nothing for that
    dimension. A missing array is NOT reported here -- that is the schema's
    "missing required property" fault, not a degenerate judgment.
    """
    empty: list[str] = []
    for field in DEGENERATE_JUDGMENT_ARRAYS:
        value = verdict.get(field)
        if isinstance(value, list) and not value:
            empty.append(field)
    return empty


def blocking_checklist_failures(verdict: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for item in verdict.get("checklist") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if item.get("blocking") is True and status in {"fail", "insufficient_evidence"}:
            failures.append(str(item.get("id") or "<unknown>"))
    return failures


def failing_acceptance_criteria(verdict: dict[str, Any]) -> list[str]:
    """Acceptance criteria whose status is ``fail`` or ``insufficient_evidence``.

    A ``complete`` verdict may not carry any such criterion: a criterion durable
    evidence shows failed (``fail``) or that lacks proof
    (``insufficient_evidence``) contradicts a ``complete`` judgment. Unlike the
    checklist, every acceptance criterion is blocking, so ``blocking`` is not
    consulted here. Each failing criterion is named so the enforcer can reject
    the self-contradictory verdict and point at the specific offender.
    """
    failures: list[str] = []
    for item in verdict.get("acceptance") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status in {"fail", "insufficient_evidence"}:
            failures.append(str(item.get("criterion") or "<unknown>"))
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


def _reason_is_transient(reason: Any) -> bool:
    """True when ``reason`` says the item's evidence was not yet available at judge
    time (a concurrent check still in flight), rather than genuinely missing."""
    if not isinstance(reason, str):
        return False
    normalized = _normalize_reason(reason)
    return any(marker in normalized for marker in TRANSIENT_NOT_YET_MARKERS)


def _offending_blocking_checklist_items(verdict: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """``(id, status, reason)`` for each blocking checklist item that makes a
    ``complete`` verdict self-contradictory (same filter as
    ``blocking_checklist_failures``, carrying status/reason for classification)."""
    items: list[tuple[str, str, Any]] = []
    for item in verdict.get("checklist") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if item.get("blocking") is True and status in {"fail", "insufficient_evidence"}:
            items.append((str(item.get("id") or "<unknown>"), status, item.get("reason")))
    return items


def _offending_acceptance_items(verdict: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """``(criterion, status, reason)`` for each acceptance criterion that makes a
    ``complete`` verdict self-contradictory (same filter as
    ``failing_acceptance_criteria``, carrying status/reason for classification)."""
    items: list[tuple[str, str, Any]] = []
    for item in verdict.get("acceptance") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status in {"fail", "insufficient_evidence"}:
            items.append((str(item.get("criterion") or "<unknown>"), status, item.get("reason")))
    return items


def _classify_transient(items: list[tuple[str, str, Any]]) -> tuple[bool, str]:
    """Transient iff EVERY offending item is ``insufficient_evidence`` whose reason
    marks its evidence not-yet-available at judge time. A durable ``fail`` or a
    genuinely-missing-evidence reason on any offending item makes the refusal
    non-transient (a re-run cannot fix a real failure or absent evidence)."""
    if not items:
        return False, "no offending item to classify"
    for identifier, status, reason in items:
        if status != "insufficient_evidence":
            return False, (
                f"{identifier} is {status}: durable evidence shows a failure, "
                "not an item whose evidence was merely not yet available at judge time"
            )
        if not _reason_is_transient(reason):
            return False, (
                f"{identifier} lacks its own durable evidence for a reason that is not a "
                "not-yet-available race; the evidence is missing, not merely in flight"
            )
    return True, (
        "every offending item is insufficient_evidence whose reason marks its evidence "
        "not yet available at judge time (the CCA ran while a required check was still in "
        "flight); a re-run after the concurrent checks settle may reach complete"
    )


def analyze_refusal(verdict: dict[str, Any]) -> tuple[list[str], bool, str]:
    """Best-effort ``(offending_ids, transient, transient_reason)`` for a rejected
    verdict, mirroring ``validate_verdict``'s rule ORDER so the offending ids and
    the transient classification describe the SAME rule that rejected it. Never
    raises: any internal error yields ``([], False, <reason>)`` so the refusal
    record is still written."""
    try:
        degenerate = degenerate_judgment_arrays(verdict)
        if degenerate:
            return (
                [degenerate[0]],
                False,
                "a present-but-empty judgment array is a structural fault, not a not-yet-available race",
            )
        try:
            schema = load_schema(Path(".shiki/schemas/cca-verdict.schema.json"))
            validate_instance(verdict, schema)
        except SchemaValidationError:
            return [], False, "a schema-invalid verdict is malformed, not a not-yet-available race"
        status = verdict.get("verdict")
        if status not in VALID_VERDICTS:
            return [], False, "an invalid verdict value is malformed, not a not-yet-available race"
        repair_packet = verdict.get("repair_packet")
        if status == "repair_required" and not isinstance(repair_packet, dict):
            return [], False, "a repair_required verdict missing its packet is malformed, not a not-yet-available race"
        if repair_packet is not None:
            try:
                repair_schema = load_schema(Path(".shiki/schemas/repair-packet.schema.json"))
                validate_instance(repair_packet, repair_schema, path="$.repair_packet")
            except SchemaValidationError:
                return [], False, "an invalid repair packet is malformed, not a not-yet-available race"
        if status == "complete":
            checklist_items = _offending_blocking_checklist_items(verdict)
            if checklist_items:
                transient, reason = _classify_transient(checklist_items)
                return [item[0] for item in checklist_items], transient, reason
            acceptance_items = _offending_acceptance_items(verdict)
            if acceptance_items:
                transient, reason = _classify_transient(acceptance_items)
                return [item[0] for item in acceptance_items], transient, reason
        short_circuited = short_circuited_evaluations(verdict)
        if short_circuited:
            return (
                short_circuited,
                False,
                "a short-circuited evaluation is a judge defect (an item left unevaluated "
                "because the verdict was already blocked), not a not-yet-available race",
            )
        return [], False, "no offending item identified"
    except Exception:  # noqa: BLE001 - analysis is best-effort; the record still gets written.
        return [], False, "the refusal could not be analyzed"


def _write_refusal_record(
    verdict: Any,
    rule_violated: str,
    offending_ids: list[str],
    transient: bool,
    transient_reason: str,
) -> Path:
    """Record a rejected verdict on the sibling path derived from ``CCA_VERDICT_FILE``.

    The record carries the verdict as received, the rule it violated, the offending
    checklist/acceptance ids, and whether the cause is transient. It is NEVER
    written to ``cca-verdict.json`` -- that path stays reserved for a verdict that
    passed validation, so a refused verdict can never become the authority the loop
    and MergeGate consume.
    """
    verdict_file = Path(os.environ.get("CCA_VERDICT_FILE", ".shiki/gha/cca-verdict.json"))
    record_path = refusal_record_path(verdict_file)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "kind": REFUSAL_RECORD_KIND,
        "verdict_as_received": verdict if isinstance(verdict, dict) else None,
        "rule_violated": rule_violated,
        "offending_ids": offending_ids,
        "transient": bool(transient),
        "transient_reason": transient_reason,
    }
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record_path


def validate_verdict(verdict: dict[str, Any]) -> None:
    degenerate = degenerate_judgment_arrays(verdict)
    if degenerate:
        field = degenerate[0]
        raise SchemaValidationError(
            f"$.{field}: CCA verdict is degenerate: the {field} array is empty; "
            f"a verdict that judged no {field} entries proves nothing and is rejected"
        )

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

    acceptance_failures = failing_acceptance_criteria(verdict)
    if status == "complete" and acceptance_failures:
        raise SchemaValidationError(
            "complete verdict contains failing acceptance criteria: " + ", ".join(acceptance_failures)
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
    # Load first, apart from validation: a parse failure has no verdict object to
    # analyze, so its refusal record carries a null verdict and a non-transient
    # (malformed) classification.
    try:
        verdict = load_verdict()
    except Exception as error:  # noqa: BLE001 - this is a CLI boundary.
        _write_refusal_record(
            None,
            f"the CCA structured output could not be parsed as JSON: {error}",
            [],
            False,
            "an unparseable verdict is malformed, not a not-yet-available race",
        )
        return fail(f"invalid CCA verdict: {error}")
    if not isinstance(verdict, dict):
        _write_refusal_record(
            None,
            "CCA verdict must be a JSON object",
            [],
            False,
            "a non-object verdict is malformed, not a not-yet-available race",
        )
        return fail("CCA verdict must be a JSON object")

    try:
        verdict = inject_authoritative_head_sha(verdict)
        validate_verdict(verdict)
    except Exception as error:  # noqa: BLE001 - this is a CLI boundary.
        # A rejected verdict leaves a readable refusal record on a NON-authority
        # path (never cca-verdict.json), classified transient/non-transient, then
        # exits non-zero so the check stays red. cca-verdict.json is written ONLY
        # below, for a verdict that passed validation.
        offending_ids, transient, transient_reason = analyze_refusal(verdict)
        _write_refusal_record(verdict, str(error), offending_ids, transient, transient_reason)
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
