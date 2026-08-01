#!/usr/bin/env python3
"""ADR 0015 Contract Approval: the pure evaluator MergeGate accepts as an
alternative to a live PR Guardian approval.

ADR 0015 moves Guardian approval for high/critical risk from the implementation
diff to the task contract, *before* dispatch. A spec-frozen Goal registers its
task contracts to the default branch through a Guardian-approved Contract PR; the
implementation PRs of that Goal then carry no approval of their own. This module
answers one question, deterministically and with no I/O:

    Is THIS PR's task contract one that was registered and Guardian-approved
    before dispatch, and left unchanged?

``evaluate_contract_approval`` returns a small result object carrying whether the
carry ``applies``, the ``sources`` recorded when it does (a single
``contract_approval`` token, reported in the same array the CCA Guardian signal
and MergeGate already use), and the ``reasons`` naming every failed condition.

ADR 0015 "Evaluation placement": Contract Approval is evaluated by this new,
separately tested function; MergeGate accepts *either* live PR approval OR a
``applies=True`` Contract Approval. ``evaluate_guardian_approval`` — the most
security-critical function in the repository — is never modified. A defect here
therefore degrades to "Contract Approval does not apply" (the current behaviour),
never to a weakening of the live-approval path. There is deliberately no
default-true branch anywhere below: every path to ``applies=True`` requires all
four conditions to hold.

The proof that a registering PR was merged and Guardian-approved is passed in as
data (``registration``, parsed by the caller from a ``--contract-approval`` JSON
file), so this function performs no I/O and no network calls and is testable in
isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Reuse the SINGLE governance-contract field list that
# ``mergegate_check.normal_task_contract_immutability_reasons`` freezes on the
# normal task-PR path. Importing the one constant (never declaring a second copy)
# is what guarantees the immutability gate and this carry can never drift: the
# fields a PR may not change are exactly the fields whose equality authorizes the
# carry. mergegate_check has no module-level import of this module (it imports
# ``evaluate_contract_approval`` lazily), so this import never forms a cycle.
from mergegate_check import _NORMAL_PATH_GOVERNANCE_FIELDS

# The token recorded in the Guardian ``sources`` array when Contract Approval
# applies. ADR 0015 is explicit that a ``contract_approval`` entry in the signal's
# existing ``sources`` array satisfies the CCA prompt's current wording.
CONTRACT_APPROVAL_SOURCE = "contract_approval"


@dataclass(frozen=True)
class ContractApprovalResult:
    """Outcome of evaluating ADR 0015 Contract Approval for one task PR.

    ``applies`` is True only when all four conditions hold; ``sources`` then
    carries exactly ``(CONTRACT_APPROVAL_SOURCE,)`` and is empty otherwise.
    ``reasons`` names every failed condition (one distinct string each), so a
    caller can report precisely why the carry did not apply.
    """

    applies: bool
    sources: tuple[str, ...]
    reasons: list[str]


def _is_pr_number(value: Any) -> bool:
    """A PR number is a positive int. ``bool`` is excluded explicitly: in Python
    ``True`` is an ``int`` (``isinstance(True, int)`` is True), and a stray boolean
    must never be read as PR #1."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _registration_proof_reasons(task_id: str, registration: Any) -> list[str]:
    """Reasons the ``registration`` proof fails to prove the registering PR
    (ADR 0015 condition 3). Empty list means the proof holds.

    The proof must: name a PR number; report ``merged: true``; report that the
    commit which ADDED ``.shiki/tasks/<task_id>.json`` to the base branch belongs
    to that PR; and report ``guardian_approved: true`` with an approving source
    recorded. Anything missing, malformed, or false is a distinct reason. No
    branch here yields "proven" by default: a non-dict / empty proof fails.
    """
    if not isinstance(registration, dict):
        return [
            f"contract approval does not apply: no registration proof was supplied for task {task_id}"
        ]

    reasons: list[str] = []

    pr_number = registration.get("pr")
    if not _is_pr_number(pr_number):
        reasons.append(
            f"contract approval does not apply: registration proof for task {task_id} does not name a "
            f"registering PR number (got {pr_number!r})"
        )

    if registration.get("merged") is not True:
        reasons.append(
            f"contract approval does not apply: registration proof for task {task_id} does not report the "
            f"registering PR merged (merged={registration.get('merged')!r})"
        )

    # The commit that added the task file to base must be identified AND must
    # belong to the named registering PR — "registration naming a different PR
    # than the adding commit" defeats the carry.
    adding_commit = registration.get("adding_commit")
    adding_commit_pr = registration.get("adding_commit_pr")
    if not isinstance(adding_commit, str) or not adding_commit.strip():
        reasons.append(
            f"contract approval does not apply: registration proof for task {task_id} does not identify the "
            f"commit that added its task file to the base branch"
        )
    if not (_is_pr_number(adding_commit_pr) and _is_pr_number(pr_number) and adding_commit_pr == pr_number):
        reasons.append(
            f"contract approval does not apply: the commit that added task {task_id} to the base branch does "
            f"not belong to the registering PR (adding_commit_pr={adding_commit_pr!r}, pr={pr_number!r})"
        )

    if registration.get("guardian_approved") is not True:
        reasons.append(
            f"contract approval does not apply: registration proof for task {task_id} does not report "
            f"guardian_approved (guardian_approved={registration.get('guardian_approved')!r})"
        )
    guardian_source = registration.get("guardian_source")
    if not isinstance(guardian_source, str) or not guardian_source.strip():
        reasons.append(
            f"contract approval does not apply: registration proof for task {task_id} records no approving "
            f"Guardian source (guardian_source={guardian_source!r})"
        )

    return reasons


def evaluate_contract_approval(
    *,
    task_id: str,
    base_task: Any,
    head_task: Any,
    changed_files_status: Any,
    registration: Any,
) -> ContractApprovalResult:
    """Whether this PR carries an ADR 0015 Contract Approval.

    The four conditions are evaluated IN ORDER, each failure recorded as a
    distinct reason string:

      1. The task file EXISTS in the base snapshot (``base_task`` is a dict). A
         first-registration PR is never covered — the contract it would carry was
         never registered to the default branch, so nothing approved it.
      2. The head task's seven governance fields
         (``_NORMAL_PATH_GOVERNANCE_FIELDS`` — the SAME list the normal-path
         immutability gate freezes) equal the base snapshot's. A changed contract
         is not the approved contract.
      3. ``registration`` proves the registering PR: it names a PR number, reports
         ``merged: true``, reports that the commit which ADDED the task file to
         base belongs to that PR, and reports ``guardian_approved: true`` with the
         approving source recorded (see ``_registration_proof_reasons``).
      4. ``registration.task_id`` equals ``task_id`` — a proof for a different
         task never carries.

    ``changed_files_status`` is accepted so the evaluation carries the same
    diff context the caller already has; the four gating conditions are decided
    from the task snapshots and the registration proof, so it does not itself
    admit or defeat the carry.

    ``applies`` is True only when NO reason was recorded. This function performs
    no I/O and no network calls: ``registration`` is the deterministic proof,
    supplied as data.
    """
    reasons: list[str] = []

    # 1. The contract must already exist on the base branch.
    base_present = isinstance(base_task, dict)
    if not base_present:
        reasons.append(
            f"contract approval does not apply: task {task_id} is absent from the base snapshot "
            f"(a first-registration PR is never covered)"
        )

    # 2. The frozen governance contract must be unchanged from base. Only checked
    #    when a base contract exists to compare against (condition 1); otherwise
    #    condition 1 has already recorded the failure.
    if base_present:
        if not isinstance(head_task, dict):
            reasons.append(
                f"contract approval does not apply: the head task for {task_id} is not a task object, so its "
                f"governance contract cannot be compared to the base snapshot"
            )
        else:
            for field in _NORMAL_PATH_GOVERNANCE_FIELDS:
                if base_task.get(field) != head_task.get(field):
                    reasons.append(
                        f"contract approval does not apply: governance field {field!r} of task {task_id} "
                        f"differs from the base snapshot (the approved contract was changed)"
                    )

    # 3. The registering PR must be proven merged and Guardian-approved.
    reasons.extend(_registration_proof_reasons(task_id, registration))

    # 4. The proof must be for THIS task.
    if isinstance(registration, dict):
        proof_task_id = registration.get("task_id")
        if proof_task_id != task_id:
            reasons.append(
                f"contract approval does not apply: registration proof task_id {proof_task_id!r} does not "
                f"match task {task_id} (a proof for a different task never carries)"
            )

    applies = not reasons
    return ContractApprovalResult(
        applies=applies,
        sources=(CONTRACT_APPROVAL_SOURCE,) if applies else (),
        reasons=reasons,
    )
