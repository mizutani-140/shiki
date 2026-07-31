#!/usr/bin/env python3
"""Deterministic MergeGate policy check for Shiki PRs."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import re
import sys
from pathlib import Path
from typing import Any

from shiki_contracts import DEFAULT_REQUIRED_CHECKS
from shiki_evidence import validate_cca_evidence_manifest
from shiki_guardian import GuardianPolicyError, evaluate_guardian_approval, load_guardian_policy_file, risk_requires_guardian, validate_guardian_policy
from shiki_jsonschema import JsonSchemaError, UnsupportedJsonSchemaError, validate_json_schema
from shiki_locks import active_lock_conflicts, files_outside_locks
from shiki_manifest import ManifestError, load_manifest
from shiki_schema import SchemaValidationError, validate_instance
from shiki_state_classes import UNKNOWN_STATE_CLASS, class_policy, classify_shiki_path


ID_SUFFIX = r"(?:[0-9]{4,}|[0-9]{8}T[0-9]{12}Z-[0-9a-f]{8})"
TASK_ID = re.compile(rf"\bT-{ID_SUFFIX}\b")
GOAL_ID = re.compile(rf"\bG-{ID_SUFFIX}\b")
SELF_CHECKS = {"MergeGate policy check"}
VERDICT_CHECKS = {"CCA verdict"}
PLACEHOLDER_CHECKS = {"shiki-required-checks"}


@dataclass(frozen=True)
class ChangedFile:
    status: str
    path: str
    old_path: str | None = None


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_json_list(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array")
    return [entry for entry in data if isinstance(entry, dict)]


def load_schema(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object schema")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_config_scalar(value: str) -> Any:
    value = value.strip().strip("\"'")
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def load_shiki_config(target: Path) -> dict[str, dict[str, Any]]:
    """Read the small .shiki/config.yaml subset MergeGate owns."""
    config_path = target / ".shiki" / "config.yaml"
    if not config_path.exists():
        return {}

    config: dict[str, dict[str, Any]] = {}
    section: str | None = None
    key: str | None = None
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if indent == 0:
            section = stripped[:-1] if stripped.endswith(":") else None
            key = None
            if section:
                config.setdefault(section, {})
            continue
        if section is None:
            continue
        if indent == 2:
            if stripped.endswith(":"):
                key = stripped[:-1]
                config[section].setdefault(key, [])
                continue
            if ":" in stripped:
                item_key, value = stripped.split(":", 1)
                config[section][item_key.strip()] = parse_config_scalar(value)
                key = None
                continue
        if indent >= 4 and key and stripped.startswith("- "):
            values = config[section].setdefault(key, [])
            if isinstance(values, list):
                values.append(parse_config_scalar(stripped[2:]))
    return config


def has_heading(body: str, heading: str) -> bool:
    return re.search(rf"^#+\s+{re.escape(heading)}\s*$", body, re.IGNORECASE | re.MULTILINE) is not None


def first_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(0) if match else None


def changed_files(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def parse_changed_files_status(path: Path, fallback_files: list[str]) -> list[ChangedFile]:
    if not path.exists():
        return [ChangedFile("M", file) for file in fallback_files]

    entries: list[ChangedFile] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) == 1:
            parts = line.split()
        status_token = parts[0]
        status = status_token[:1].upper()
        if status == "R" and len(parts) >= 3:
            entries.append(ChangedFile("D", parts[1]))
            entries.append(ChangedFile("A", parts[2], old_path=parts[1]))
        elif status in {"A", "M", "D"} and len(parts) >= 2:
            entries.append(ChangedFile(status, parts[1]))
        elif len(parts) >= 2:
            entries.append(ChangedFile(status or "M", parts[1]))
    return entries


def ledger_entry_text(entry: dict[str, Any]) -> str:
    parts: list[str] = [
        str(entry.get("type") or ""),
        str(entry.get("summary") or ""),
        " ".join(str(value) for value in entry.get("evidence") or []),
        " ".join(str(value) for value in entry.get("links") or []),
    ]
    return "\n".join(parts).lower()


def load_task(target: Path, task_id: str) -> dict[str, Any] | None:
    return load_json(target / ".shiki" / "tasks" / f"{task_id}.json")


def load_goal(target: Path, goal_id: str) -> dict[str, Any] | None:
    return load_json(target / ".shiki" / "goals" / f"{goal_id}.json")


def load_dag(target: Path, goal_id: str) -> dict[str, Any] | None:
    return load_json(target / ".shiki" / "dag" / f"{goal_id}.json")


# The .shiki subtrees the goal loop syncs onto a task branch, keyed by the id
# they are scoped to. Task-scoped: the task file, its worktree record, its lock
# record. Goal-scoped: the goal file and its DAG (shared, read-only goal state).
_MIRROR_TASK_SUBTREES: tuple[str, ...] = ("tasks", "worktrees", "locks")
_MIRROR_GOAL_SUBTREES: tuple[str, ...] = ("goals", "dag")


def _derive_task_mirror_locks(target: Path, task: dict[str, Any]) -> list[str]:
    """The task's id-scoped .shiki mirror lock set, recomputed at judgment time.

    A registered task's stored ``locks`` are EXACTLY what the plan declared —
    nothing is injected. But the goal loop syncs the task's own .shiki evidence
    onto its branch (shiki_loop._evidence_relatives_for_task): its task file,
    worktree record and lock record (task-id scoped); its goal file and DAG
    (goal-id scoped); the ledger entries it owns; AND every ``.shiki``-prefixed
    path those ledgers' ``evidence`` arrays reference (a ``type:check`` gate ledger
    -> ``.shiki/runner/EXEC-*.json``; a completion ledger ->
    ``.shiki/reports/R-*.json``). MergeGate's files-outside-locks gate must accept
    those mutations, so this recomputes the covering set from the task's own ids
    and unions it into the effective locks. Deriving at read time (not injecting at
    registration) is the point: the loop appends ledgers and writes runner/EXEC
    records AFTER dispatch, so any coverage written into the stored locks would be
    stale by the time MergeGate reads it.

    The derivation is BOUNDED so a PR-authored task file cannot widen its own
    effective locks by citing someone else's evidence: a ledger in
    ``ledger_evidence`` is expanded ONLY when its own ``goal_id`` matches this
    task's goal AND its ``task_id`` is either this task's id or empty. An empty
    ``task_id`` means a goal-level ledger of this goal — e.g. the completion
    ledger ``cmd_goal_complete`` writes for a closeout, whose evidence cites
    ``.shiki/reports/R-*.json`` — which the loop syncs onto the branch. Any other
    reference — a foreign goal's ledger, one owned by a DIFFERENT task of this
    goal, a missing/malformed one, or an evidence ref that escapes the ``.shiki``
    subtree — contributes nothing. Every lock is a concrete single-file path
    (never a directory glob over a shared mirror namespace). Order-stable,
    de-duplicated.
    """
    task_id = str(task.get("id") or "")
    goal_id = str(task.get("goal_id") or "")
    locks: list[str] = []
    if task_id:
        for subtree in _MIRROR_TASK_SUBTREES:
            locks.append(f"path:.shiki/{subtree}/{task_id}.json")
    if goal_id:
        for subtree in _MIRROR_GOAL_SUBTREES:
            locks.append(f"path:.shiki/{subtree}/{goal_id}.json")

    shiki_root = (target / ".shiki").resolve()
    for ledger_id in task.get("ledger_evidence") or []:
        ledger_id = str(ledger_id)
        if not ledger_id:
            continue
        try:
            entry = load_json(target / ".shiki" / "ledger" / f"{ledger_id}.json")
        except (OSError, ValueError):
            # Missing/malformed ledger: contribute nothing (fail open on read,
            # matching the loop's own sync, and never widening).
            continue
        if not isinstance(entry, dict):
            continue
        # Bound to the task's OWN goal: a foreign goal_id is ignored so a
        # PR-authored task file cannot pad ledger_evidence with someone else's
        # ledger to inherit the .shiki paths its evidence references. A ledger
        # explicitly owned by a DIFFERENT task of this goal is also ignored (the
        # anti-padding property). But a ledger with an EMPTY task_id is a
        # goal-level ledger of this goal — e.g. cmd_goal_complete's type:completion
        # entry, whose evidence cites .shiki/reports/R-*.json — and the loop syncs
        # it onto the closeout branch, so it is accepted.
        entry_goal = str(entry.get("goal_id") or "")
        entry_task = str(entry.get("task_id") or "")
        if entry_goal != goal_id:
            continue
        if entry_task and entry_task != task_id:
            continue
        locks.append(f"path:.shiki/ledger/{ledger_id}.json")
        for ref in entry.get("evidence") or []:
            ref = str(ref)
            if not ref.startswith(".shiki/"):
                continue
            # Containment: a prefix check alone does not stop traversal
            # ('.shiki/../x' passes startswith but resolves outside). Resolve and
            # require the path to stay within .shiki, exactly as the loop's sync does.
            candidate = (target / ref).resolve()
            try:
                candidate.relative_to(shiki_root)
            except ValueError:
                continue
            locks.append(f"path:{ref}")
    return list(dict.fromkeys(locks))


def goal_reconcile_decision(pr: dict[str, Any]) -> tuple[bool, str | None]:
    """Decide whether a PR runs in goal_reconcile mode.

    Requires BOTH the body marker (declares intent) AND a maintainer-applied
    label (authorizes the relaxed scope). A marker without the label fails closed
    with an error so untrusted PR text cannot self-grant the relaxed mode.
    """
    body = str(pr.get("body") or "")
    marker = bool(GOAL_RECONCILE_MARKER.search(body))
    label = GOAL_RECONCILE_LABEL in pr_label_names(pr)
    if marker and not label:
        return False, (
            f"goal_reconcile requires the {GOAL_RECONCILE_LABEL} label (a maintainer-applied "
            "second factor) in addition to the body marker"
        )
    return (marker and label), None


def load_plan(target: Path, plan_id: str) -> dict[str, Any] | None:
    return load_json(target / ".shiki" / "plans" / f"{plan_id}.json")


# A goal_reconcile PR declares itself with this exact HTML-comment marker. A
# comment marker (not a heading) is required because heading detection is
# substring-permissive and forgeable; the marker is precise and cannot be
# accidentally triggered by prose.
GOAL_RECONCILE_MARKER = re.compile(r"<!--\s*shiki:goal_reconcile\s*-->")
# Relaxed reconcile scope must not be self-authorizable from the PR body alone.
# A second factor — this label, which only a write-access maintainer/Guardian can
# apply — is required in addition to the body marker.
GOAL_RECONCILE_LABEL = "mergegate:goal_reconcile"


# A Contract PR (ADR 0015) declares itself with this exact HTML-comment marker.
# It carries a spec-frozen Goal's task contracts (goal, task, and DAG
# registration) to the default branch so a Guardian can approve them before any
# implementation is dispatched. Like goal_reconcile, the marker only DECLARES
# intent; the maintainer-applied label below is the independent second factor.
CONTRACT_MARKER = re.compile(r"<!--\s*shiki:contract\s*-->")
# The contract mode's second factor. Only a write-access maintainer/Guardian can
# apply it, so contract mode cannot be self-granted from the PR body. The CLI
# that opens a Contract PR must NOT apply this label to its own PR (ADR 0015
# bootstrap note): self-applying it collapses the two factors to one. The CLI
# mirrors this exact value (scripts/shiki_tasks.py CONTRACT_PR_LABEL); a
# consistency test binds the two so they cannot drift.
CONTRACT_LABEL = "mergegate:contract"


def contract_decision(pr: dict[str, Any]) -> tuple[bool, str | None]:
    """Decide whether a PR runs in contract mode (marker + label).

    Requires BOTH the body marker (declares intent) AND the maintainer-applied
    label (authorizes the mode). A marker without the label fails closed with an
    error so untrusted PR text cannot self-grant the relaxed registration mode.
    """
    body = str(pr.get("body") or "")
    marker = bool(CONTRACT_MARKER.search(body))
    label = CONTRACT_LABEL in pr_label_names(pr)
    if marker and not label:
        return False, (
            f"contract mode requires the {CONTRACT_LABEL} label (a maintainer-applied "
            "second factor) in addition to the body marker"
        )
    return (marker and label), None


# Governance-relevant fields of a frozen plan task that, when present in the
# plan, a goal_reconcile-registered task MUST match exactly. Plan tasks carry no
# ids, so title is the lookup key; everything else binds the task definition so a
# reconcile cannot register a frozen title with a weakened risk/locks/criteria.
# (Plan `runtime` -> task `assigned_runtime` and plan title-based `dependencies`
# are representation-mismatched and are not field-compared here.)
_FROZEN_TASK_MATCH_FIELDS = (
    "scope",
    "non_goals",
    "required_skills",
    "risk_level",
    "locks",
    "acceptance_checks",
)


def _frozen_plan_tasks(target: Path, goal_id: str, mode: str = "goal_reconcile") -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Return (frozen tasks by title, errors) for a goal's spec-frozen source plan.

    The frozen authority is the goal's source_plan with spec_freeze.status=frozen.
    Plan tasks are keyed by title (no ids in the plan schema). Duplicate titles
    fail closed, because title is the only binding key and an ambiguous key would
    let a registration match the wrong frozen definition. ``mode`` names the
    caller (goal_reconcile or contract) in blocking messages.
    """
    errors: list[str] = []
    goal = load_goal(target, goal_id)
    if goal is None:
        return {}, [f"{mode}: goal {goal_id} not found"]
    plan_id = str(goal.get("source_plan") or "")
    if not plan_id:
        return {}, [f"{mode}: goal {goal_id} has no source_plan to bind to"]
    plan = load_plan(target, plan_id)
    if plan is None:
        return {}, [f"{mode}: source_plan {plan_id} not found"]
    spec_freeze = plan.get("spec_freeze")
    if not isinstance(spec_freeze, dict) or spec_freeze.get("status") != "frozen":
        return {}, [f"{mode}: source_plan {plan_id} is not spec-frozen"]
    tasks: dict[str, dict[str, Any]] = {}
    for t in plan.get("tasks") or []:
        if not isinstance(t, dict) or not t.get("title"):
            continue
        title = str(t.get("title")).strip()
        if title in tasks:
            errors.append(f"{mode}: frozen plan {plan_id} has duplicate task title {title!r}; cannot bind")
            continue
        tasks[title] = t
    return tasks, errors


def _frozen_task_match_errors(
    task_id: str, task: dict[str, Any], frozen: dict[str, Any], mode: str = "goal_reconcile"
) -> list[str]:
    """A registered task must match its frozen plan task on every governance
    field the plan declares (frozen-definition binding, not just title)."""
    errors: list[str] = []
    for field in _FROZEN_TASK_MATCH_FIELDS:
        if field in frozen and task.get(field) != frozen.get(field):
            errors.append(
                f"{mode} task {task_id} field {field!r} does not match its frozen plan definition"
            )
    # Runtime assignment is governance-relevant (it picks the execution adapter):
    # the frozen plan's `runtime` must equal the registered task's
    # `assigned_runtime` (the field names differ; the value must match).
    if frozen.get("runtime") and task.get("assigned_runtime") != frozen.get("runtime"):
        errors.append(
            f"{mode} task {task_id} assigned_runtime {task.get('assigned_runtime')!r} "
            f"does not match the frozen plan runtime {frozen.get('runtime')!r}"
        )
    return errors


def _enforce_frozen_registration(
    *,
    mode: str,
    target: Path,
    goal_id: str,
    changed_files_status: list[ChangedFile],
    blocking: list[str],
    warnings: list[str],
    allow_goal_registration: bool,
    allow_source_plan_carry: bool = False,
) -> None:
    """Validate a frozen-plan registration PR (goal_reconcile or contract mode).

    Deny by default. The PR may ONLY: add planned task files whose title is in the
    goal's spec-frozen plan (each title covered at most once) and whose goal_id is
    this goal; restore the goal's DAG so every node resolves to a frozen-plan task
    anchored to this goal; append a goal-scoped task-registered ledger; and, when
    ``allow_goal_registration`` is set (contract mode, ADR 0015), add the goal
    file itself — matched against its frozen plan. Everything else — code, marking
    a task done/cancelled/superseded, modifying an existing task (e.g.
    acceptance_checks), goal-complete reports, locks, repairs, runner records,
    memories, authority artifacts (guardian-policy, distilled rules), or the
    frozen plan — is forbidden. In goal_reconcile mode the goal file is also
    forbidden (its source_plan binding and status must not move). This relaxes the
    per-task one-file rule into a narrow, frozen-plan-bound registration mode,
    never a general multi-file bypass.
    """
    if not goal_id:
        blocking.append(f"{mode} PR must reference a Shiki goal id")
        return
    frozen_tasks, frozen_errors = _frozen_plan_tasks(target, goal_id, mode)
    if frozen_errors:
        blocking.extend(frozen_errors)
        return
    frozen_titles = set(frozen_tasks)
    # The goal's source-plan id, read from the goal at HEAD (the contract PR may
    # register the goal in this same diff, or it may already be on base). Contract
    # mode (ADR 0015) may carry the Goal's own spec-frozen source plan to the
    # default branch, because nothing else puts it there and the mode otherwise
    # deadlocks: `_frozen_plan_tasks` blocks with "source_plan not found" when the
    # plan is absent, and the deny-by-default `else` blocks "must not change" when
    # the PR carries it. See ``_validate_contract_source_plan``.
    goal_for_plan = load_goal(target, goal_id)
    source_plan_id = (
        str(goal_for_plan.get("source_plan") or "") if isinstance(goal_for_plan, dict) else ""
    )

    goal_file = f".shiki/goals/{goal_id}.json"
    dag_file = f".shiki/dag/{goal_id}.json"
    reconcile_ledger_seen = False
    # Each frozen plan title may be registered exactly once. Seed the consumed
    # set with titles of tasks ALREADY registered for the goal (excluding the
    # ones this PR adds, which are present in the HEAD checkout) so a reconcile
    # cannot re-register or double-cover a frozen plan task.
    added_task_ids = {
        Path(normalize_repo_path(e.path)).stem
        for e in changed_files_status
        if e.status == "A" and normalize_repo_path(e.path).startswith(".shiki/tasks/") and e.path.endswith(".json")
    }
    consumed_titles: set[str] = set()
    tasks_dir = target / ".shiki" / "tasks"
    if tasks_dir.is_dir():
        for existing in tasks_dir.glob("T-*.json"):
            if existing.stem in added_task_ids:
                continue
            existing_data = load_json(existing)
            if isinstance(existing_data, dict) and str(existing_data.get("goal_id") or "") == goal_id:
                consumed_titles.add(str(existing_data.get("title") or "").strip())

    for entry in changed_files_status:
        path = normalize_repo_path(entry.path)
        if not path.startswith(".shiki/"):
            blocking.append(f"{mode} must not change non-Shiki (implementation) file {path}")
            continue
        if entry.status == "D":
            blocking.append(f"{mode} must not delete {path}")
            continue
        if path.startswith(".shiki/tasks/") and path.endswith(".json"):
            if entry.status != "A":
                blocking.append(f"{mode} may only ADD planned task files, not modify {path}")
                continue
            data = load_json(target / path)
            if not isinstance(data, dict):
                blocking.append(f"{mode} task file {path} is not a JSON object")
                continue
            task_id = Path(path).stem
            if str(data.get("id") or "") != task_id:
                blocking.append(f"{mode} task filename {path} does not match its id {data.get('id')!r}")
            if str(data.get("goal_id") or "") != goal_id:
                blocking.append(f"{mode} task {task_id} is not anchored to goal {goal_id}")
            if data.get("status") != "planned":
                blocking.append(f"{mode} task {task_id} must be registered as status=planned, not {data.get('status')!r}")
            title = str(data.get("title") or "").strip()
            if title not in frozen_titles:
                blocking.append(f"{mode} task {task_id} title {title!r} is not in the goal's frozen plan")
            elif title in consumed_titles:
                blocking.append(f"{mode} task {task_id} re-registers frozen plan title {title!r} (already covered)")
            else:
                consumed_titles.add(title)
                # Bind to the FROZEN TASK DEFINITION, not just the title: a
                # registered task must match every governance field the frozen
                # plan declares (risk_level, locks, acceptance_checks, ...).
                blocking.extend(_frozen_task_match_errors(task_id, data, frozen_tasks[title], mode))
        elif path == dag_file:
            # DAG restore is allowed; every node must resolve to a frozen-plan
            # task ANCHORED TO THIS GOAL (existing or added in this PR), so a
            # foreign task with a colliding title cannot be wired in (poisoning).
            dag = load_json(target / path)
            nodes = dag.get("nodes") if isinstance(dag, dict) else None
            if not isinstance(nodes, list):
                blocking.append(f"{mode} DAG {path} must have a nodes list")
                continue
            node_titles: set[str] = set()
            for node in nodes:
                node_task = load_task(target, str(node))
                if node_task is None:
                    blocking.append(f"{mode} DAG node {node} has no task file")
                    continue
                if str(node_task.get("goal_id") or "") != goal_id:
                    blocking.append(f"{mode} DAG node {node} is not anchored to goal {goal_id}")
                    continue
                node_title = str(node_task.get("title") or "").strip()
                if node_title not in frozen_titles:
                    blocking.append(f"{mode} DAG node {node} title {node_title!r} is not in the goal's frozen plan")
                else:
                    node_titles.add(node_title)
            # The restored DAG must COVER the full frozen plan, not a subset: a
            # truncated DAG (dropping frozen tasks) would later let validate force
            # premature goal-complete once the registered subset finishes, while
            # frozen tasks that were never registered are silently abandoned.
            missing = frozen_titles - node_titles
            if missing:
                blocking.append(
                    f"{mode} DAG must cover every frozen plan task; missing {sorted(missing)}"
                )
        elif path.startswith(".shiki/dag/") and path.endswith(".json"):
            blocking.append(f"{mode} must not change another goal's DAG {path}")
        elif path.startswith(".shiki/ledger/") and path.endswith(".json"):
            if entry.status != "A":
                blocking.append(f"{mode} must append, not modify, ledger {path}")
                continue
            led = load_json(target / path)
            if not isinstance(led, dict) or str(led.get("goal_id") or "") != goal_id:
                blocking.append(f"{mode} ledger {path} must be scoped to goal {goal_id}")
                continue
            # The registration event must be a task-registered ledger, not any
            # goal-scoped ledger, so the requirement proves an actual registration.
            if led.get("type") == "task-registered":
                reconcile_ledger_seen = True
        elif allow_goal_registration and path == goal_file:
            # Contract mode only (ADR 0015): the Contract PR registers the goal
            # for the first time. Validate it against its frozen plan; deny a
            # modify/delete or a risk downgrade that would weaken the Guardian
            # gate this contract exists to force.
            _validate_contract_goal_registration(target, goal_id, entry, blocking, mode)
        elif path.startswith(".shiki/plans/") and path.endswith(".json"):
            # Contract flow only (ADR 0015): the Contract PR may carry the Goal's
            # own spec-frozen source plan to the default branch, gated by the
            # explicit ``allow_source_plan_carry`` opt-in. This is the sole plan
            # mutation any frozen-registration PR may make, and it is deny-by-
            # default: only ADDING (status A) the goal's source_plan, and only when
            # its content is a spec-frozen plan. A MODIFY, a plan that is not the
            # goal's source_plan, or an unfrozen plan is blocked — so a PR can never
            # author or alter a spec_freeze block on any other plan through this
            # relaxation. goal_reconcile never opts in (the frozen plan is the
            # immutable base authority it validates against), so no plan mutation
            # is allowed there at all.
            _validate_contract_source_plan(
                target=target,
                source_plan_id=source_plan_id,
                entry=entry,
                blocking=blocking,
                mode=mode,
                allow_source_plan=allow_source_plan_carry,
            )
        else:
            # Deny by default. In goal_reconcile mode the goal file itself is
            # forbidden here: a reconcile must not move the goal's source_plan
            # binding (it is validated against), change its status, or touch any
            # other field. Also forbidden in both modes: memories, reports,
            # guardian-policy, locks, repairs, runner, plans, and everything else.
            blocking.append(
                f"{mode} must not change {path}; only frozen-plan task registration, "
                f"DAG restore for {goal_id}, and a task-registered reconcile ledger are allowed"
            )

    if not reconcile_ledger_seen:
        blocking.append(f"{mode} must include a goal-scoped task-registered reconcile ledger event for {goal_id}")

    # HEAD invariant (independent of whether the DAG file is in this diff): after
    # the reconcile, the goal's DAG must cover every frozen-plan task. Without
    # this, a PR could register a subset of tasks and simply OMIT the DAG; a
    # legacy DAG-less goal would then let validate force premature goal-complete
    # on the registered subset, abandoning the unregistered frozen tasks. This
    # makes "the reconcile restores the goal's DAG" a hard invariant.
    head_dag = load_dag(target, goal_id)
    head_nodes = head_dag.get("nodes") if isinstance(head_dag, dict) else None
    covered_titles: set[str] = set()
    title_to_id: dict[str, str] = {}
    if isinstance(head_nodes, list):
        for node in head_nodes:
            node_task = load_task(target, str(node))
            if isinstance(node_task, dict) and str(node_task.get("goal_id") or "") == goal_id:
                node_title = str(node_task.get("title") or "").strip()
                covered_titles.add(node_title)
                title_to_id[node_title] = str(node)
    head_missing = frozen_titles - covered_titles
    if head_missing:
        blocking.append(
            f"{mode} must leave the goal's DAG covering every frozen plan task; missing {sorted(head_missing)}"
        )

    # Bind frozen dependency semantics to the DAG edges: the frozen plan declares
    # dependencies by title; the restored DAG edges must be EXACTLY the set of
    # those dependencies mapped to registered task ids (from -> dependency, to ->
    # dependent). Otherwise a reconcile could register all tasks/nodes but drop or
    # invent dependency edges, weakening the dependency-done ordering the DAG
    # encodes. Only checked once every frozen task is covered (ids resolvable).
    if not head_missing:
        expected_edges: set[tuple[str, str]] = set()
        # Per dependent task id, the exact set of dependency task ids the frozen
        # plan declares — used to bind BOTH the DAG edges AND the task file's own
        # `dependencies` field (the normal MergeGate path gates dependency-done
        # from task.dependencies, so the DAG alone is insufficient).
        expected_task_deps: dict[str, set[str]] = {tid: set() for tid in title_to_id.values()}
        edge_errors: list[str] = []
        for title, frozen in frozen_tasks.items():
            for dep_title in frozen.get("dependencies") or []:
                dep_title = str(dep_title).strip()
                if dep_title not in title_to_id:
                    edge_errors.append(
                        f"{mode} frozen dependency {dep_title!r} of task {title!r} does not resolve to a registered task"
                    )
                    continue
                expected_edges.add((title_to_id[dep_title], title_to_id[title]))
                expected_task_deps[title_to_id[title]].add(title_to_id[dep_title])
        blocking.extend(edge_errors)
        head_edges: set[tuple[str, str]] = set()
        for edge in (head_dag.get("edges") if isinstance(head_dag, dict) else None) or []:
            if isinstance(edge, dict) and edge.get("from") and edge.get("to"):
                head_edges.add((str(edge["from"]), str(edge["to"])))
        if not edge_errors and head_edges != expected_edges:
            missing_edges = expected_edges - head_edges
            extra_edges = head_edges - expected_edges
            blocking.append(
                f"{mode} DAG edges must match the frozen plan dependencies; "
                f"missing {sorted(missing_edges)}, unexpected {sorted(extra_edges)}"
            )
        # Bind each registered task's own `dependencies` field to the frozen plan
        # (mapped to ids), so the normal dependency-done gate that reads
        # task.dependencies cannot be bypassed by a divergent/absent field.
        if not edge_errors:
            for tid, expected_deps in expected_task_deps.items():
                tdata = load_task(target, tid)
                actual_deps = {str(d) for d in (tdata.get("dependencies") if isinstance(tdata, dict) else None) or []}
                if actual_deps != expected_deps:
                    blocking.append(
                        f"{mode} task {tid} dependencies {sorted(actual_deps)} do not match the frozen plan "
                        f"{sorted(expected_deps)}"
                    )


def enforce_goal_reconcile(
    *,
    target: Path,
    goal_id: str,
    changed_files_status: list[ChangedFile],
    blocking: list[str],
    warnings: list[str],
) -> None:
    """Validate a goal_reconcile PR (drift recovery): register spec-frozen tasks
    into main state without touching the goal file. See
    ``_enforce_frozen_registration``."""
    _enforce_frozen_registration(
        mode="goal_reconcile",
        target=target,
        goal_id=goal_id,
        changed_files_status=changed_files_status,
        blocking=blocking,
        warnings=warnings,
        allow_goal_registration=False,
    )


def enforce_contract(
    *,
    target: Path,
    goal_id: str,
    changed_files_status: list[ChangedFile],
    blocking: list[str],
    warnings: list[str],
    allow_source_plan_carry: bool = False,
) -> None:
    """Validate a Contract PR (ADR 0015): register a spec-frozen Goal's task
    contracts — goal, task, and DAG registration — to the default branch before
    dispatch. A generalization of goal_reconcile that ALSO allows the Contract PR
    to register the goal file itself (first registration), matched against its
    frozen plan. Deny by default for everything else. Guardian evaluation is
    forced from the Goal's frozen-plan risk in ``main`` (see
    ``contract_guardian_risk``).

    ``allow_source_plan_carry`` opts into the ADR 0015 plan-reachability escape:
    the Contract PR may ADD the Goal's own spec-frozen source plan (see
    ``_validate_contract_source_plan``). It defaults off so the relaxation is never
    automatic; ``main``'s contract flow enables it because nothing else puts the
    frozen plan on the default branch."""
    _enforce_frozen_registration(
        mode="contract",
        target=target,
        goal_id=goal_id,
        changed_files_status=changed_files_status,
        blocking=blocking,
        warnings=warnings,
        allow_goal_registration=True,
        allow_source_plan_carry=allow_source_plan_carry,
    )


def _validate_contract_goal_registration(
    target: Path, goal_id: str, entry: ChangedFile, blocking: list[str], mode: str
) -> None:
    """Validate the goal file a Contract PR registers for the first time.

    The goal must be ADDED (never modified/deleted — an existing goal contract is
    immutable), be status=planned, bind to a spec-frozen source_plan, and carry a
    risk_level that matches that frozen plan. The risk match is load-bearing: the
    Guardian gate is forced from the goal's frozen-plan risk, so a goal file that
    understates its risk would silently weaken the gate."""
    if entry.status != "A":
        blocking.append(
            f"{mode} may only ADD the goal registration file .shiki/goals/{goal_id}.json, not modify/delete it"
        )
        return
    goal = load_json(target / f".shiki/goals/{goal_id}.json")
    if not isinstance(goal, dict):
        blocking.append(f"{mode} goal registration .shiki/goals/{goal_id}.json is not a JSON object")
        return
    if str(goal.get("id") or "") != goal_id:
        blocking.append(f"{mode} goal registration filename does not match its id {goal.get('id')!r}")
    if goal.get("status") != "planned":
        blocking.append(f"{mode} goal {goal_id} must be registered as status=planned, not {goal.get('status')!r}")
    plan_id = str(goal.get("source_plan") or "")
    plan = load_plan(target, plan_id) if plan_id else None
    if not isinstance(plan, dict):
        blocking.append(f"{mode} goal {goal_id} source_plan {plan_id!r} does not resolve to a plan")
        return
    # A registered contract MUST declare a canonical risk level: the Guardian gate
    # is forced from it, and a missing/unknown value would resolve to no gate
    # (fail-open). Requiring it here means enforce_contract itself rejects a
    # risk-less goal, independent of any separate validate_shiki run.
    if goal.get("risk_level") not in _RISK_ORDER:
        blocking.append(
            f"{mode} goal {goal_id} must declare a canonical risk_level "
            f"(one of {sorted(_RISK_ORDER)}), not {goal.get('risk_level')!r}"
        )
    if goal.get("risk_level") != plan.get("risk_level"):
        blocking.append(
            f"{mode} goal {goal_id} risk_level {goal.get('risk_level')!r} does not match its frozen plan "
            f"definition risk_level {plan.get('risk_level')!r}"
        )


def _validate_contract_source_plan(
    *,
    target: Path,
    source_plan_id: str,
    entry: ChangedFile,
    blocking: list[str],
    mode: str,
    allow_source_plan: bool,
) -> None:
    """Validate the spec-frozen source plan a Contract PR carries (ADR 0015).

    ADR 0015 has a Contract PR carry only Goal/task/DAG registration and assumes
    the Goal's frozen plan already sits on the default branch, but nothing puts it
    there — so a Contract PR for any new Goal deadlocks: ``_frozen_plan_tasks``
    blocks "source_plan not found" when the plan is absent, and the deny-by-default
    branch blocks "must not change" when the PR carries it. This permits exactly
    one escape from that deadlock and nothing wider: ADDING the Goal's own
    source_plan whose content is a spec-frozen plan.

    The carry is an explicit OPT-IN (``allow_source_plan`` — the contract flow in
    ``main`` enables it; goal_reconcile and any bare validator call do not), so the
    relaxation is never granted automatically and the frozen-registration
    validator stays deny-by-default. When opted in it is still deny-by-default in
    substance: blocked are a MODIFY/DELETE of a plan (which would author or alter a
    spec_freeze block on an already-present plan), a plan whose id is not the
    Goal's source_plan, and a plan that is not spec-frozen. The Guardian gate this
    contract forces from the Goal's frozen-plan risk is what authorizes the carried
    freeze — the content check only proves it is a well-formed frozen source plan.
    """
    path = normalize_repo_path(entry.path)
    plan_id = Path(path).stem
    if not allow_source_plan:
        blocking.append(
            f"{mode} must not change {path}; carrying the Goal's frozen source plan "
            "is not enabled for this registration"
        )
        return
    if entry.status != "A":
        blocking.append(
            f"{mode} may only ADD the Goal's spec-frozen source plan, not modify {path}"
        )
        return
    if not source_plan_id:
        blocking.append(
            f"{mode} cannot resolve the Goal's source_plan, so the carried plan {path} is not admissible"
        )
        return
    if plan_id != source_plan_id:
        blocking.append(
            f"{mode} may only carry the Goal's source_plan {source_plan_id}, not {plan_id}"
        )
        return
    plan = load_plan(target, plan_id)
    if not isinstance(plan, dict):
        blocking.append(f"{mode} source plan {path} is not a JSON object")
        return
    if str(plan.get("id") or "") != plan_id:
        blocking.append(
            f"{mode} source plan filename {path} does not match its id {plan.get('id')!r}"
        )
    spec_freeze = plan.get("spec_freeze")
    if not isinstance(spec_freeze, dict) or spec_freeze.get("status") != "frozen":
        blocking.append(
            f"{mode} source plan {path} must be spec-frozen (spec_freeze.status=frozen) to be carried"
        )


# Risk ordering, low to high. Used to force the Guardian gate from the STRONGER
# of a Contract PR's goal-file risk and its immutable frozen-plan risk, so a
# downgraded goal file can never resolve to a weaker gate than the frozen plan.
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _max_risk(first: str, second: str) -> str:
    return first if _RISK_ORDER.get(first, -1) >= _RISK_ORDER.get(second, -1) else second


def contract_guardian_risk(target: Path, goal_id: str) -> str | None:
    """Resolve the risk level that forces the Guardian gate for a Contract PR.

    The authority is the goal's spec-frozen source_plan (immutable, on the base
    branch), never the mutable goal file alone. Returns the stronger of the frozen
    plan's risk and the goal file's risk, or None when the goal cannot be
    resolved (in which case the contract validator has already blocked)."""
    goal = load_goal(target, goal_id)
    if not isinstance(goal, dict):
        return None
    goal_risk = str(goal.get("risk_level") or "")
    plan_id = str(goal.get("source_plan") or "")
    plan = load_plan(target, plan_id) if plan_id else None
    plan_risk = str(plan.get("risk_level") or "") if isinstance(plan, dict) else ""
    resolved = _max_risk(plan_risk, goal_risk)
    return resolved or None


POST_MERGE_RECONCILE_MARKER = re.compile(r"<!--\s*shiki:post_merge_reconcile\s*-->")
POST_MERGE_RECONCILE_LABEL = "mergegate:post_merge_reconcile"
# Only these task fields may move during a post-merge reconcile of a merged task.
_POST_MERGE_TASK_FIELDS = {"status", "expected_pr", "ledger_evidence"}
# The reconcile may only set the task to 'review' — NOT a terminal status. If it
# marked a goal's last DAG node 'done', validate_shiki on the same HEAD would
# demand the goal be 'complete' (all DAG nodes terminal), which the mode forbids
# touching — re-deadlocking the final task. Leaving it 'review' keeps the node
# non-terminal and lets the existing goal-complete flow close the goal.
_POST_MERGE_TASK_STATUSES = {"review"}


def post_merge_reconcile_decision(pr: dict[str, Any]) -> tuple[bool, str | None]:
    """Decide whether a PR runs in post_merge_reconcile mode (marker + label)."""
    body = str(pr.get("body") or "")
    marker = bool(POST_MERGE_RECONCILE_MARKER.search(body))
    label = POST_MERGE_RECONCILE_LABEL in pr_label_names(pr)
    if marker and not label:
        return False, (
            f"post_merge_reconcile requires the {POST_MERGE_RECONCILE_LABEL} label (a maintainer-applied "
            "second factor) in addition to the body marker"
        )
    return (marker and label), None


def enforce_post_merge_reconcile(
    *,
    target: Path,
    task_id: str,
    base_shiki: Path | None,
    changed_files_status: list[ChangedFile],
    blocking: list[str],
    warnings: list[str],
    merged_pr_numbers: set[int] | None = None,
    pr_body: str | None = None,
) -> None:
    """Validate a post_merge_reconcile PR: reconcile a MERGED task's residual
    state. Deny by default.

    The autonomous loop normally releases a task's lock and marks it done the
    moment its PR merges; when that did not happen (e.g. a manual merge), the
    residual lock and expected_pr block all later control-plane work. This mode
    lets one PR reconcile EXACTLY that residue for a single task whose PR is
    PROVEN merged: it must clear the task's expected_pr, must release the task's
    lock (state=released or delete) when a lock file exists, may set status to
    review/done, and appends a reconcile ledger. It may NOT touch code, other
    tasks, the goal file, the DAG, memories, reports, guardian-policy, other
    locks, or any other task field (acceptance_checks, locks, scope, …). Without
    proof the referenced PR merged, or if the residue is not actually cleaned up,
    it fails closed — never a generic status-mutation mode.
    """
    if not task_id:
        blocking.append("post_merge_reconcile PR must reference a Shiki task id")
        return
    # The PR body must reference EXACTLY ONE Shiki task id. Otherwise the merge
    # proof (computed by the workflow) and the task this validator mutates could
    # resolve to different ids, breaking the 1:1 binding between "the PR proven
    # merged" and "the task whose lock/status is cleared".
    if pr_body is not None:
        body_task_ids = {m.group(0) for m in TASK_ID.finditer(pr_body)}
        if len(body_task_ids) > 1:
            blocking.append(
                f"post_merge_reconcile PR body must reference exactly one task id; found {sorted(body_task_ids)}"
            )
            return
    head_task = load_task(target, task_id)
    if head_task is None:
        blocking.append(f"post_merge_reconcile: task {task_id} not found")
        return
    if base_shiki is None:
        blocking.append("post_merge_reconcile requires a base .shiki snapshot to verify task-field immutability")
        return
    base_task = load_json(base_shiki / "tasks" / f"{task_id}.json")
    if not isinstance(base_task, dict):
        blocking.append(f"post_merge_reconcile: base snapshot has no task {task_id} to reconcile against")
        return
    # The reconcile target must be a task whose PR ACTUALLY MERGED. The base
    # (pre-reconcile) expected_pr names that PR; merged_pr_numbers proves it
    # merged. Without that proof this mode fails closed (it is not a generic
    # status-mutation mode).
    base_expected_pr = base_task.get("expected_pr")
    # No proof set (None) is treated as "no proof" and fails closed, so the
    # function never fail-opens on its own default for a future/library caller.
    proven_merged = merged_pr_numbers or set()
    if not base_expected_pr:
        blocking.append(f"post_merge_reconcile: task {task_id} has no merged PR (no base expected_pr) to reconcile")
    else:
        try:
            base_pr_num = int(base_expected_pr)
        except (TypeError, ValueError):
            # A malformed base expected_pr becomes a deterministic blocker, not
            # an opaque CI crash.
            blocking.append(f"post_merge_reconcile: task {task_id} base expected_pr {base_expected_pr!r} is not a PR number")
            base_pr_num = None
        if base_pr_num is not None and base_pr_num not in proven_merged:
            blocking.append(
                f"post_merge_reconcile: task {task_id} references PR #{base_expected_pr}, which is not proven merged"
            )
    # The residue MUST be cleaned up: expected_pr cleared on the head task.
    if head_task.get("expected_pr") is not None:
        blocking.append(f"post_merge_reconcile must clear expected_pr on task {task_id} (still {head_task.get('expected_pr')!r})")

    task_file = f".shiki/tasks/{task_id}.json"
    lock_file = f".shiki/locks/{task_id}.json"
    reconcile_ledger_seen = False
    lock_reconciled = False
    # If a lock file exists for the task, the reconcile MUST release it; a stale
    # active lock is exactly the residue this mode exists to clear.
    lock_exists_at_head = (target / ".shiki" / "locks" / f"{task_id}.json").is_file()

    for entry in changed_files_status:
        path = normalize_repo_path(entry.path)
        if not path.startswith(".shiki/"):
            blocking.append(f"post_merge_reconcile must not change non-Shiki (implementation) file {path}")
            continue
        if path == task_file:
            if entry.status != "M":
                blocking.append(f"post_merge_reconcile may only modify its task file {path}, not add/delete it")
                continue
            for key in set(base_task) | set(head_task):
                if key not in _POST_MERGE_TASK_FIELDS and base_task.get(key) != head_task.get(key):
                    blocking.append(f"post_merge_reconcile must not change task field {key!r} of {task_id}")
            if head_task.get("status") not in _POST_MERGE_TASK_STATUSES:
                blocking.append(f"post_merge_reconcile task {task_id} status must be review (not terminal), not {head_task.get('status')!r}")
        elif path == lock_file:
            if entry.status == "D":
                lock_reconciled = True
                continue  # deleting the lock is a valid release
            lock = load_json(target / path)
            if not (isinstance(lock, dict) and lock.get("state") == "released"):
                blocking.append(f"post_merge_reconcile lock change to {path} must set state=released (or delete the lock)")
            else:
                lock_reconciled = True
        elif path.startswith(".shiki/ledger/") and path.endswith(".json"):
            if entry.status != "A":
                blocking.append(f"post_merge_reconcile must append, not modify, ledger {path}")
                continue
            led = load_json(target / path)
            led_ok = isinstance(led, dict) and led.get("task_id") == task_id and led.get("type") in {"lock", "check"}
            if not led_ok:
                blocking.append(f"post_merge_reconcile ledger {path} must be a task-scoped lock/check reconcile event for {task_id}")
                continue
            reconcile_ledger_seen = True
        else:
            blocking.append(
                f"post_merge_reconcile must not change {path}; only this task's status/expected_pr, its lock release, "
                "and a reconcile ledger are allowed"
            )

    if not reconcile_ledger_seen:
        blocking.append(f"post_merge_reconcile must include a task-scoped reconcile ledger event for {task_id}")
    # A stale active lock is the residue this mode exists to clear: if a lock
    # file exists for the task, the PR must release it.
    if lock_exists_at_head and not lock_reconciled:
        blocking.append(f"post_merge_reconcile must release the task lock {lock_file} (it is the residue being reconciled)")


def blocking_checklist_failures(verdict: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for item in verdict.get("checklist") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if item.get("blocking") is True and status in {"fail", "insufficient_evidence"}:
            failures.append(str(item.get("id") or "<unknown>"))
    return failures


def validate_cca_contract(target: Path, cca: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        schema = load_schema(target / ".shiki" / "schemas" / "cca-verdict.schema.json")
        validate_instance(cca, schema)
    except (OSError, ValueError, SchemaValidationError) as error:
        errors.append(str(error))

    repair_packet = cca.get("repair_packet")
    if repair_packet is not None:
        try:
            repair_schema = load_schema(target / ".shiki" / "schemas" / "repair-packet.schema.json")
            validate_instance(repair_packet, repair_schema, path="$.repair_packet")
        except (OSError, ValueError, SchemaValidationError) as error:
            errors.append(str(error))

    return errors


def validate_cca_evidence_contract(
    *,
    target: Path,
    manifest_path: Path,
    evidence_dir: Path,
    pr: dict[str, Any],
    task_id: str | None,
    goal_id: str | None,
    expected_repository: str,
) -> list[str]:
    errors: list[str] = []
    if not manifest_path.exists():
        return [f"CCA evidence manifest file not found at {manifest_path}"]
    if not expected_repository:
        return ["Expected repository is required for CCA evidence manifest validation"]
    try:
        manifest = load_json(manifest_path)
        if not isinstance(manifest, dict):
            return [f"{manifest_path}: CCA evidence manifest must be a JSON object"]
        schema = load_schema(target / ".shiki" / "schemas" / "cca-evidence-manifest.schema.json")
        validate_json_schema(manifest, schema)
    except (OSError, ValueError, JsonSchemaError, UnsupportedJsonSchemaError) as error:
        return [f"CCA evidence manifest schema violation: {error}"]

    pr_number = pr.get("number")
    head_sha = pr.get("headRefOid")
    if not isinstance(pr_number, int):
        errors.append("PR number is missing; cannot validate CCA evidence manifest")
    if not isinstance(head_sha, str) or not head_sha:
        errors.append("PR headRefOid is missing; cannot validate CCA evidence manifest")
    if errors:
        return errors

    errors.extend(
        validate_cca_evidence_manifest(
            manifest=manifest,
            evidence_dir=evidence_dir,
            expected_repo=expected_repository,
            expected_pr=pr_number,
            expected_head_sha=head_sha,
            expected_task_id=task_id,
            expected_goal_id=goal_id,
        )
    )
    return errors


def configured_required_checks(target: Path) -> list[str]:
    mergegate = load_shiki_config(target).get("mergegate", {})
    checks = [str(check) for check in mergegate.get("required_checks") or [] if str(check).strip()]
    return checks or DEFAULT_REQUIRED_CHECKS


def configured_required_review(target: Path) -> bool:
    defaults = load_shiki_config(target).get("defaults", {})
    value = defaults.get("required_review")
    if isinstance(value, bool):
        return value
    return True


def workflow_job_names(target: Path) -> set[str]:
    names: set[str] = set()
    workflow_dir = target / ".github" / "workflows"
    if not workflow_dir.exists():
        return names
    for path in sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml")):
        in_jobs = False
        in_job = False
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip() or raw_line.lstrip().startswith("#"):
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            stripped = raw_line.strip()
            if indent == 0:
                in_jobs = stripped == "jobs:"
                in_job = False
                continue
            if in_jobs and indent == 2 and stripped.endswith(":"):
                in_job = True
                continue
            if in_jobs and in_job and indent == 4 and stripped.startswith("name:"):
                name = stripped.split(":", 1)[1].strip().strip("\"'")
                if name:
                    names.add(name)
    return names


def enforce_required_check_definitions(target: Path, blocking: list[str]) -> None:
    jobs = workflow_job_names(target)
    if not jobs:
        blocking.append("No GitHub workflow job names are available for required check validation")
        return
    for check in configured_required_checks(target):
        if check in PLACEHOLDER_CHECKS:
            continue
        if check not in jobs:
            blocking.append(f"Required check {check} is not defined by workflow job names")


def pr_label_names(pr: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for label in pr.get("labels") or []:
        if isinstance(label, dict):
            name = label.get("name")
        else:
            name = label
        if name:
            names.add(str(name).strip().lower())
    return names


def status_checks(pr: dict[str, Any]) -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    for check in pr.get("statusCheckRollup") or []:
        if not isinstance(check, dict):
            continue
        name = check.get("name") or check.get("workflowName") or check.get("context")
        if name:
            checks[str(name)] = check
    return checks


def check_head_sha(check: dict[str, Any]) -> str | None:
    value = check.get("headSha") or check.get("head_sha") or check.get("sha")
    if value:
        return str(value)
    commit = check.get("commit")
    if isinstance(commit, dict):
        oid = commit.get("oid") or commit.get("sha")
        if oid:
            return str(oid)
    return None


def enforce_required_checks(pr: dict[str, Any], target: Path, blocking: list[str], warnings: list[str]) -> None:
    checks = status_checks(pr)
    head_sha = pr.get("headRefOid")
    required = [
        check
        for check in configured_required_checks(target)
        if check not in SELF_CHECKS and check not in VERDICT_CHECKS and check not in PLACEHOLDER_CHECKS
    ]
    for name in required:
        check = checks.get(name)
        if check is None:
            blocking.append(f"Required check {name} is missing from PR statusCheckRollup")
            continue
        status = str(check.get("status") or "").upper()
        conclusion = str(check.get("conclusion") or "").upper()
        if status != "COMPLETED":
            warnings.append(f"Required check {name} is not completed in prepared rollup: status={status or 'UNKNOWN'}; relying on branch protection freshness")
        elif conclusion != "SUCCESS":
            blocking.append(f"Required check {name} is not successful: conclusion={conclusion or 'UNKNOWN'}")
        check_sha = check_head_sha(check)
        if head_sha and check_sha and check_sha != head_sha:
            blocking.append(f"Required check {name} head SHA {check_sha} does not match PR headRefOid {head_sha}")
        elif head_sha and not check_sha:
            warnings.append(f"Required check {name} did not expose a head SHA; relying on GitHub rollup freshness")


def review_approved(pr: dict[str, Any]) -> bool:
    if str(pr.get("reviewDecision") or "").upper() == "APPROVED":
        return True
    for review in pr.get("reviews") or []:
        if isinstance(review, dict) and str(review.get("state") or "").upper() == "APPROVED":
            return True
    return False


def enforce_review_policy(pr: dict[str, Any], target: Path, blocking: list[str]) -> None:
    review_decision = str(pr.get("reviewDecision") or "").upper()
    if review_decision == "CHANGES_REQUESTED":
        blocking.append("PR review requested changes")
    for review in pr.get("reviews") or []:
        if isinstance(review, dict) and str(review.get("state") or "").upper() == "CHANGES_REQUESTED":
            author = review.get("author") or {}
            login = author.get("login") if isinstance(author, dict) else author
            suffix = f" by {login}" if login else ""
            blocking.append(f"PR review requested changes{suffix}")
    for thread in pr.get("reviewThreads") or []:
        if isinstance(thread, dict) and thread.get("isResolved") is False:
            blocking.append("PR has unresolved review findings")

    labels = pr_label_names(pr)
    explicit_review_required = bool(labels.intersection({"review:required", "requires-review", "needs-review"}))
    if (configured_required_review(target) or explicit_review_required or review_decision == "REVIEW_REQUIRED") and not review_approved(pr):
        blocking.append("Required review is missing")


def _guardian_policy_path(target: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else target / path


def _guardian_risk_labels(
    pr: dict[str, Any], task: dict[str, Any] | None, *, bookkeeping_closeout: bool = False
) -> list[str]:
    labels = list(pr_label_names(pr))
    if task:
        risk = str(task.get("risk_level") or "").strip().lower()
        if bookkeeping_closeout:
            # ADR 0017: a PROVEN bookkeeping closeout carries no implementation and
            # inherits no new risk, so the TASK's risk is evaluated as if low at this
            # single Guardian decision point. PR labels (an independent
            # maintainer-applied signal) are left untouched, so a maintainer can
            # still force the gate by labelling the PR.
            risk = "low"
        if risk:
            labels.append(risk)
            labels.append(f"risk:{risk}")
    return labels


def _builtin_guardian_risk_required(risk_labels: list[str]) -> bool:
    normalized = {label.strip().lower().removeprefix("risk:") for label in risk_labels if label.strip()}
    return bool(normalized.intersection({"high", "critical"}))


# Task-file keys a bookkeeping closeout may legitimately move between the base
# snapshot and the PR head. Every OTHER key is the frozen governance contract
# (scope, non_goals, required_skills, risk_level, locks, acceptance_checks,
# test_command, title, dependencies, …) and must be byte-identical, so the
# exemption can never ride a smuggled contract edit.
_CLOSEOUT_MUTABLE_TASK_FIELDS = frozenset({"status", "expected_pr", "closeout_pr", "ledger_evidence"})


def _closeout_completes_goal(target: Path, goal_id: str) -> bool | None:
    """Whether this closeout completes its goal: every task node of the goal's DAG
    is ``done`` at HEAD (this task set ``done`` by the closeout). Returns None when
    it cannot be established (missing/empty DAG, or an unresolvable node) so the
    caller fails closed. The DAG is read at HEAD, but a bookkeeping closeout may
    not change the DAG (``is_bookkeeping_closeout`` rejects any ``.shiki/dag`` path
    in the diff), so the DAG this reads is the authoritative base DAG."""
    dag = load_dag(target, goal_id)
    nodes = dag.get("nodes") if isinstance(dag, dict) else None
    if not isinstance(nodes, list) or not nodes:
        return None
    for node in nodes:
        node_task = load_task(target, str(node))
        if not isinstance(node_task, dict):
            return None
        if str(node_task.get("status")) != "done":
            return False
    return True


def is_bookkeeping_closeout(
    *,
    target: Path,
    task: dict[str, Any] | None,
    task_id: str,
    goal_id: str,
    base_shiki: Path | None,
    changed_files_status: list[ChangedFile],
    merged_pr_numbers: set[int],
) -> bool:
    """Classify whether a PR is a pure bookkeeping closeout (ADR 0017).

    A bookkeeping closeout carries ONLY the terminal state that a merged task's
    completion writes to ``main``: the task moves ``review -> done``, its lock
    ``active -> released``, and — ONLY when this task completes its goal — the goal
    moves ``-> complete`` with exactly one added scorecard report. It contains zero
    implementation, so it needed no code review to produce and inherits no new
    risk; a qualifying PR is therefore evaluated as if the task were low risk at
    the single Guardian decision point (``enforce_guardian_policy``), while every
    OTHER gate runs unchanged.

    Every condition is proven from the diff, the base snapshot, and live PR state
    (``merged_pr_numbers``) — NEVER from the PR body, a label, a marker, or any
    task field the PR head can write (note: this function never reads the PR at
    all). It FAILS CLOSED on every axis: any condition that cannot be established
    (missing base snapshot, unreadable task file, unresolvable implementation PR,
    missing DAG, …) returns False, and the PR inherits the task's real risk exactly
    as before.

    The six ADR 0017 conditions:
      1. every changed path is under ``.shiki/`` (one byte outside disqualifies);
      2. no path is deleted;
      3. every changed path is inside the task's declared locks unioned with its
         derived id-scoped mirror locks (the existing files-outside-locks rule);
      4. the task file's governance fields are byte-identical to the base snapshot
         — only status, expected_pr, closeout_pr and ledger_evidence may differ;
      5. the transitions are exactly the terminal set: task ``review -> done``,
         lock ``active -> released``, and ONLY when this task completes its goal,
         goal ``-> complete`` with exactly one added scorecard report;
      6. the task's implementation PR (named by the BASE snapshot's expected_pr,
         which the PR head cannot write) is proven merged.
    """
    try:
        if base_shiki is None or not base_shiki.exists():
            return False
        if not isinstance(task, dict) or str(task.get("id") or "") != task_id or not goal_id:
            return False
        base_task = load_json(base_shiki / "tasks" / f"{task_id}.json")
        if not isinstance(base_task, dict):
            return False

        # Condition 4: every task-file key outside the mutable set is the frozen
        # governance contract and must equal the base snapshot exactly.
        for key in set(base_task) | set(task):
            if key in _CLOSEOUT_MUTABLE_TASK_FIELDS:
                continue
            if base_task.get(key) != task.get(key):
                return False

        # Condition 5 (task leg): status transitions review -> done, exactly.
        if str(base_task.get("status")) != "review" or str(task.get("status")) != "done":
            return False

        # Condition 6: the implementation PR is named by the BASE expected_pr (a
        # field the PR head cannot write) and must be proven merged.
        try:
            impl_pr_num = int(base_task.get("expected_pr"))
        except (TypeError, ValueError):
            return False
        if impl_pr_num not in (merged_pr_numbers or set()):
            return False

        # Whether this closeout completes its goal, judged from the HEAD DAG.
        completes_goal = _closeout_completes_goal(target, goal_id)
        if completes_goal is None:
            return False

        task_file = f".shiki/tasks/{task_id}.json"
        lock_file = f".shiki/locks/{task_id}.json"
        goal_file = f".shiki/goals/{goal_id}.json"
        worktree_file = f".shiki/worktrees/{task_id}.json"

        task_changed = False
        lock_released = False
        goal_completed = False
        reports_added = 0
        changed_paths: list[str] = []

        for entry in changed_files_status:
            path = normalize_repo_path(entry.path)
            old_path = normalize_repo_path(entry.old_path or "")
            # Condition 1: every changed path (both legs of a rename) under .shiki/.
            for candidate in (candidate for candidate in (path, old_path) if candidate):
                if not candidate.startswith(".shiki/"):
                    return False
                changed_paths.append(candidate)
            # Condition 2: no deletion (a rename splits into a delete leg too).
            if entry.status == "D":
                return False

            if path == task_file:
                task_changed = True
            elif path == lock_file:
                # Condition 5 (lock leg): active -> released, proven against base.
                base_lock = load_json(base_shiki / "locks" / f"{task_id}.json")
                head_lock = load_json(target / path)
                if not (isinstance(base_lock, dict) and base_lock.get("state") == "active"):
                    return False
                if not (isinstance(head_lock, dict) and head_lock.get("state") == "released"):
                    return False
                lock_released = True
            elif path == goal_file:
                # Condition 5 (goal leg): a goal transition is allowed ONLY when
                # this task completes its goal, and must be -> complete.
                if not completes_goal:
                    return False
                base_goal = load_json(base_shiki / "goals" / f"{goal_id}.json")
                head_goal = load_json(target / path)
                if not (isinstance(head_goal, dict) and str(head_goal.get("status")) == "complete"):
                    return False
                if isinstance(base_goal, dict) and str(base_goal.get("status")) == "complete":
                    # Already complete on base: not a real completion transition.
                    return False
                goal_completed = True
            elif path.startswith(".shiki/reports/") and path.endswith(".json"):
                # A scorecard is ADDED by goal completion; a modified report is not
                # part of the terminal set.
                if entry.status != "A":
                    return False
                reports_added += 1
            elif path == worktree_file:
                # This task's own worktree record is benign id-scoped mirror state
                # (no governance transition); condition 3 still covers it.
                pass
            elif path.startswith(".shiki/ledger/") and path.endswith(".json"):
                # Ledgers are append-only mirror bookkeeping (condition 3 and the
                # untrusted-mutation gate police them); an ADD carries no transition,
                # but a modify is never part of a closeout's terminal set.
                if entry.status != "A":
                    return False
            else:
                # Any other .shiki path (dag, memories, guardian-policy, another
                # task/goal/lock, a plan, …) is not part of the terminal set.
                return False

        # Condition 3: every changed path inside the task's declared locks unioned
        # with its derived id-scoped mirror locks. HEAD locks equal base locks
        # (frozen by condition 4), so a PR cannot widen them to sneak a file in.
        declared_locks = [str(lock) for lock in task.get("locks") or []]
        effective_locks = list(dict.fromkeys([*declared_locks, *_derive_task_mirror_locks(target, task)]))
        if files_outside_locks(changed_paths, effective_locks):
            return False

        # The task's review -> done transition is the closeout; its lock release is
        # a merged task's residue. Both are mandatory.
        if not task_changed or not lock_released:
            return False

        if completes_goal:
            # A completing closeout MUST carry the goal -> complete transition and
            # EXACTLY ONE added scorecard report.
            if not goal_completed or reports_added != 1:
                return False
        else:
            # A non-completing closeout must carry NO goal transition and NO report.
            if goal_completed or reports_added != 0:
                return False

        return True
    except (OSError, ValueError, TypeError):
        # Unreadable/malformed evidence is fail-closed: NOT a bookkeeping closeout.
        return False


def _load_guardian_evidence(
    *,
    path: str,
    target: Path,
    required: bool,
    blocking: list[str],
    warnings: list[str],
    description: str,
) -> list[dict[str, Any]]:
    evidence_path = Path(path)
    if not evidence_path.is_absolute():
        evidence_path = target / evidence_path
    if not evidence_path.exists():
        message = f"Guardian {description} evidence file is missing at {evidence_path}"
        if required:
            blocking.append(message)
        else:
            warnings.append(message)
        return []
    try:
        return load_json_list(evidence_path)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        message = f"Guardian {description} evidence file is invalid: {error}"
        if required:
            blocking.append(message)
        else:
            warnings.append(message)
        return []


def enforce_guardian_policy(
    *,
    pr: dict[str, Any],
    task: dict[str, Any] | None,
    target: Path,
    guardian_policy: str,
    guardian_comments: str,
    guardian_events: str,
    guardian_timeline: str,
    blocking: list[str],
    warnings: list[str],
    expected_repository: str = "",
    bookkeeping_closeout: bool = False,
) -> None:
    risk_labels = _guardian_risk_labels(pr, task, bookkeeping_closeout=bookkeeping_closeout)
    requires_guardian = _builtin_guardian_risk_required(risk_labels)
    try:
        policy = load_guardian_policy_file(_guardian_policy_path(target, guardian_policy))
    except GuardianPolicyError as error:
        if requires_guardian:
            blocking.append(f"Guardian policy is required for high/critical risk PRs: {error}")
        else:
            warnings.append(f"Guardian policy could not be loaded; skipping Guardian check for non-high-risk PR: {error}")
        return
    policy_errors = validate_guardian_policy(policy)
    if policy_errors:
        if requires_guardian:
            blocking.extend(f"Guardian policy validation failed: {error}" for error in policy_errors)
        else:
            warnings.extend(f"Guardian policy validation warning: {error}" for error in policy_errors)
        return
    requires_guardian = requires_guardian or risk_requires_guardian(risk_labels, policy)
    if not requires_guardian:
        return

    blocker_count_before_evidence = len(blocking)
    comments = _load_guardian_evidence(
        path=guardian_comments,
        target=target,
        required=True,
        blocking=blocking,
        warnings=warnings,
        description="comments",
    )
    events = _load_guardian_evidence(
        path=guardian_events,
        target=target,
        required=True,
        blocking=blocking,
        warnings=warnings,
        description="label events",
    )
    timeline = _load_guardian_evidence(
        path=guardian_timeline,
        target=target,
        required=False,
        blocking=blocking,
        warnings=warnings,
        description="timeline",
    )
    if len(blocking) > blocker_count_before_evidence:
        return

    reviews = [review for review in pr.get("reviews") or [] if isinstance(review, dict)]
    head_sha = str(pr.get("headRefOid") or "")
    result = evaluate_guardian_approval(
        policy=policy,
        pr=pr,
        reviews=reviews,
        comments=comments,
        label_events=events + timeline,
        head_sha=head_sha,
        expected_repo=expected_repository,
    )
    warnings.extend(result.warnings)
    if not result.approved:
        blocking.extend(result.blockers or ("Guardian approval is required but policy-backed evidence is missing",))
    else:
        approver_desc = ", ".join(result.approvers) if result.approvers else ""
        if result.ai_reviewers:
            # External AI guardian review: record the AI reviewer identity
            # distinctly (reviewer_type=external_ai_model), never as a human.
            ai_desc = "reviewer_type=external_ai_model (" + ", ".join(result.ai_reviewers) + ")"
            approver_desc = (approver_desc + "; " + ai_desc) if approver_desc else ai_desc
        warnings.append(
            "Guardian approval satisfied by "
            + ", ".join(result.sources)
            + " from "
            + (approver_desc or "<unknown>")
        )


def load_ledger_entries(target: Path, task: dict[str, Any], warnings: list[str], blocking: list[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for ledger_id in task.get("ledger_evidence") or []:
        if not isinstance(ledger_id, str):
            blocking.append("Task ledger_evidence contains a non-string entry")
            continue
        path = target / ".shiki" / "ledger" / f"{ledger_id}.json"
        entry = load_json(path)
        if entry is None:
            blocking.append(f"Task ledger evidence {ledger_id} is missing at {path}")
            continue
        entries.append(entry)
    if not entries:
        blocking.append("Task has no ledger evidence entries")
    return entries


def normalize_repo_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def runtime_evidence_path(path: str) -> bool:
    normalized = normalize_repo_path(path)
    name = Path(normalized).name
    return (
        normalized.startswith(".shiki/gha/")
        or normalized.startswith(".shiki/cca/")
        or (normalized.startswith(".shiki/") and name.startswith("cca-verdict") and name.endswith(".json"))
        or (normalized.startswith(".shiki/") and name.startswith("mergegate-result") and name.endswith(".json"))
    )


def shiki_json_id(path: Path) -> str | None:
    data = load_json(path)
    if not data:
        return None
    value = data.get("id") or data.get("repair_id")
    return str(value) if value else None


def protected_base_files(base_shiki: Path, subdir: str) -> dict[str, Path]:
    root = base_shiki / subdir
    if not root.exists():
        return {}
    return {
        f".shiki/{subdir}/{path.name}": path
        for path in sorted(root.glob("*.json"))
        if path.is_file()
    }


def file_bytes(path: Path) -> bytes | None:
    if not path.exists() or not path.is_file():
        return None
    return path.read_bytes()


def ledger_entry_allowed_for_task(entry: dict[str, Any], *, task_id: str, goal_id: str) -> bool:
    entry_task = str(entry.get("task_id") or "").strip()
    entry_goal = str(entry.get("goal_id") or "").strip()
    if entry_task == task_id:
        return True
    return not entry_task and entry_goal == goal_id


def enforce_untrusted_shiki_mutations(
    *,
    target: Path,
    manifest: dict[str, Any] | None,
    base_shiki: Path | None,
    changed_files_status: list[ChangedFile],
    task: dict[str, Any],
    goal_id: str,
    task_id: str,
    pr: dict[str, Any],
    blocking: list[str],
    warnings: list[str],
) -> None:
    task_file = f".shiki/tasks/{task_id}.json"
    goal_file = f".shiki/goals/{goal_id}.json"
    lock_file = f".shiki/locks/{task_id}.json"
    allowed_ledger_ids = {str(value) for value in task.get("ledger_evidence") or []}
    task_locks = {str(value) for value in task.get("locks") or []}
    pr_number = pr.get("number")

    for entry in changed_files_status:
        path = normalize_repo_path(entry.path)
        old_path = normalize_repo_path(entry.old_path or "")
        paths_to_check = [candidate for candidate in [path, old_path] if candidate]

        for candidate in paths_to_check:
            state_class = classify_shiki_path(candidate, manifest or {}) if candidate.startswith(".shiki/") else ""
            policy = class_policy(state_class, manifest or {}) if state_class else {}
            is_runtime_evidence = runtime_evidence_path(candidate)
            if is_runtime_evidence:
                blocking.append(
                    f"Runtime CCA/MergeGate evidence path {candidate} must come from workflow artifacts, not PR files; state_class={state_class or 'workflow-runtime-evidence'}"
                )
            if candidate.startswith(".shiki/") and state_class == UNKNOWN_STATE_CLASS:
                blocking.append(f"Unknown Shiki state path {candidate}; state_class={state_class}")
                continue
            if state_class in {"workflow-runtime-evidence", "cache", "local-only"} or policy.get("pr_mutation") == "forbidden":
                blocking.append(f"PR must not change {candidate}; state_class={state_class}")

        if path.startswith(".shiki/tasks/") and path.endswith(".json"):
            state_class = classify_shiki_path(path, manifest or {})
            if entry.status == "D":
                blocking.append(f"PR must not delete Shiki task file {path}; state_class={state_class}")
            elif path != task_file:
                blocking.append(f"PR changes unrelated Shiki task file {path}; expected only {task_file}; state_class={state_class}")

        if path.startswith(".shiki/goals/") and path.endswith(".json"):
            state_class = classify_shiki_path(path, manifest or {})
            if entry.status == "D":
                blocking.append(f"PR must not delete Shiki goal file {path}; state_class={state_class}")
            elif path != goal_file:
                blocking.append(f"PR changes unrelated Shiki goal file {path}; expected only {goal_file}; state_class={state_class}")

        if path.startswith(".shiki/locks/") and path.endswith(".json"):
            state_class = classify_shiki_path(path, manifest or {})
            if path != lock_file:
                blocking.append(f"PR changes unrelated Shiki lock file {path}; expected only {lock_file}; state_class={state_class}")

        if path.startswith(".shiki/ledger/") and path.endswith(".json"):
            state_class = classify_shiki_path(path, manifest or {})
            ledger_id = Path(path).stem
            ledger_path = target / path
            if entry.status == "D":
                blocking.append(f"PR must not delete Shiki ledger file {path}; state_class={state_class}")
                continue
            if entry.status != "A":
                blocking.append(f"PR must append new Shiki ledger evidence instead of modifying {path}; state_class={state_class}")
                continue
            if ledger_id not in allowed_ledger_ids:
                blocking.append(f"PR changes ledger {ledger_id} not listed in current task ledger_evidence; state_class={state_class}")
                continue
            ledger = load_json(ledger_path)
            if ledger is None:
                blocking.append(f"Changed ledger file {path} is not readable; state_class={state_class}")
                continue
            if str(ledger.get("id") or "") != ledger_id:
                blocking.append(f"Ledger filename {path} does not match JSON id {ledger.get('id')!r}; state_class={state_class}")
            if not ledger_entry_allowed_for_task(ledger, task_id=task_id, goal_id=goal_id):
                blocking.append(f"Ledger {ledger_id} is not scoped to task {task_id} or goal {goal_id}; state_class={state_class}")

        if path.startswith(".shiki/repairs/") and path.endswith(".json"):
            repair_path = target / path
            repair = load_json(repair_path)
            repair_id = Path(path).stem
            if repair is None:
                blocking.append(f"Changed repair packet {path} is not readable")
                continue
            if str(repair.get("repair_id") or repair.get("id") or repair_id) != repair_id:
                blocking.append(f"Repair filename {path} does not match JSON id")
            if str(repair.get("task_id") or "") != task_id:
                blocking.append(f"Repair packet {repair_id} is not scoped to task {task_id}")
            repair_pr = repair.get("pr")
            if repair_pr != pr_number:
                blocking.append(f"Repair packet {repair_id} does not reference current PR #{pr_number}")

    if base_shiki is None:
        warnings.append("No base .shiki snapshot provided; protected base-state comparison was skipped")
        return
    if not base_shiki.exists():
        warnings.append(f"Base .shiki snapshot not found at {base_shiki}; protected base-state comparison was skipped")
        return

    for base_path, source_path in protected_base_files(base_shiki, "ledger").items():
        state_class = classify_shiki_path(base_path, manifest or {})
        target_path = target / base_path
        if not target_path.exists():
            blocking.append(f"PR must not delete base ledger file {base_path}; state_class={state_class}")
            continue
        if file_bytes(source_path) != file_bytes(target_path):
            blocking.append(f"PR must not modify existing base ledger file {base_path}; state_class={state_class}")

    for subdir in ["tasks", "goals"]:
        for base_path in protected_base_files(base_shiki, subdir):
            state_class = classify_shiki_path(base_path, manifest or {})
            if not (target / base_path).exists():
                blocking.append(f"PR must not delete base Shiki {subdir[:-1]} file {base_path}; state_class={state_class}")

    for base_path in protected_base_files(base_shiki, "locks"):
        if base_path == lock_file:
            continue
        if not (target / base_path).exists():
            state_class = classify_shiki_path(base_path, manifest or {})
            blocking.append(f"PR must not delete unrelated base Shiki lock file {base_path}; state_class={state_class}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Shiki MergeGate readiness")
    parser.add_argument("--target", default=".", help="Target repository path")
    parser.add_argument("--pr-json", default=".shiki/gha/pr.json")
    parser.add_argument("--cca-verdict", default=".shiki/gha/cca-verdict.json")
    parser.add_argument("--cca-evidence-manifest", default="")
    parser.add_argument("--changed-files", default=".shiki/gha/changed-files.txt")
    parser.add_argument("--changed-files-status", default=".shiki/gha/changed-files-status.txt")
    parser.add_argument("--expected-repository", default="")
    parser.add_argument("--expected-head-sha")
    parser.add_argument("--base-shiki")
    parser.add_argument(
        "--merged-prs",
        default="",
        help="Comma-separated PR numbers proven merged (used by post_merge_reconcile to verify the referenced PR actually merged).",
    )
    parser.add_argument("--guardian-policy", default=".shiki/guardian-policy.json")
    parser.add_argument("--guardian-comments", default=".shiki/gha/live-guardian-comments.json")
    parser.add_argument("--guardian-events", default=".shiki/gha/live-guardian-events.json")
    parser.add_argument("--guardian-timeline", default=".shiki/gha/live-guardian-timeline.json")
    parser.add_argument("--result-file", default=".shiki/gha/mergegate-result.json")
    parser.add_argument("--allow-missing-cca", action="store_true")
    args = parser.parse_args()

    target = Path(args.target).resolve()
    blocking: list[str] = []
    warnings: list[str] = []
    resolved_task_id: str | None = None
    resolved_goal_id: str | None = None
    reconcile_mode = False
    post_merge_mode = False
    contract_mode = False
    # PR numbers proven merged (live PR state gathered by the workflow). Used by
    # post_merge_reconcile to prove its referenced PR merged, and by the bookkeeping
    # closeout classifier to prove the task's implementation PR merged (ADR 0017).
    merged_prs = {
        int(token.strip())
        for token in str(args.merged_prs or "").split(",")
        if token.strip().isdigit()
    }
    # Whether the (normal single-task) PR is a proven bookkeeping closeout, so the
    # Guardian gate evaluates it as if the task were low risk (ADR 0017). Stays
    # False for contract / goal_reconcile / post_merge_reconcile PRs.
    bookkeeping_closeout = False
    manifest: dict[str, Any] | None = None
    try:
        manifest = load_manifest(target)
    except ManifestError as error:
        blocking.append(str(error))

    pr = load_json(Path(args.pr_json))
    body = ""
    if pr:
        body = str(pr.get("body") or "")
        resolved_task_id = first_match(TASK_ID, body)
        resolved_goal_id = first_match(GOAL_ID, body)
        reconcile_mode, reconcile_error = goal_reconcile_decision(pr)
        post_merge_mode, post_merge_error = post_merge_reconcile_decision(pr)
        contract_mode, contract_error = contract_decision(pr)
        if reconcile_error:
            blocking.append(reconcile_error)
        if post_merge_error:
            blocking.append(post_merge_error)
        if contract_error:
            blocking.append(contract_error)
        if reconcile_mode and post_merge_mode:
            blocking.append("a PR cannot be both goal_reconcile and post_merge_reconcile")
        if contract_mode and reconcile_mode:
            blocking.append("a PR cannot be both contract and goal_reconcile")
        if contract_mode and post_merge_mode:
            blocking.append("a PR cannot be both contract and post_merge_reconcile")
        if args.expected_head_sha:
            pr_head = str(pr.get("headRefOid") or "")
            if not pr_head:
                blocking.append("PR headRefOid is missing")
            elif pr_head != args.expected_head_sha:
                blocking.append(f"PR headRefOid {pr_head} does not match expected checked-out HEAD {args.expected_head_sha}")
        if reconcile_mode:
            # A goal_reconcile PR is goal-scoped (frozen-plan task registration);
            # it does not carry a single implementation task id.
            if not resolved_goal_id:
                blocking.append("goal_reconcile PR body does not contain a Shiki goal id like G-0001")
        elif contract_mode:
            # A Contract PR is goal-scoped (frozen-plan task-contract registration);
            # it carries no single implementation task id (ADR 0015).
            if not resolved_goal_id:
                blocking.append("contract PR body does not contain a Shiki goal id like G-0001")
        elif post_merge_mode:
            # A post_merge_reconcile PR is task-scoped (reconcile a merged task).
            if not resolved_task_id:
                blocking.append("post_merge_reconcile PR body does not contain a Shiki task id like T-0001")
        else:
            if not resolved_task_id:
                blocking.append("PR body does not contain a Shiki task id like T-0001")
            if not resolved_goal_id:
                blocking.append("PR body does not contain a Shiki goal id like G-0001")
        for heading in ["Scope", "Acceptance", "Evidence", "MergeGate"]:
            if heading.lower() not in body.lower() and not has_heading(body, heading):
                blocking.append(f"PR body is missing {heading} section")
    else:
        warnings.append(f"PR JSON not found at {args.pr_json}; skipping PR metadata checks")
    enforce_required_check_definitions(target, blocking)

    task: dict[str, Any] | None = None
    ledger_entries: list[dict[str, Any]] = []
    if reconcile_mode and pr:
        # goal_reconcile is a goal-scoped, frozen-plan registration PR; it uses a
        # dedicated deny-by-default validator instead of the single-task readiness
        # flow (no implementation task, CCA, locks, or guardian gate).
        files_status = parse_changed_files_status(
            Path(args.changed_files_status), changed_files(Path(args.changed_files))
        )
        enforce_goal_reconcile(
            target=target,
            goal_id=resolved_goal_id or "",
            changed_files_status=files_status,
            blocking=blocking,
            warnings=warnings,
        )
    elif contract_mode and pr:
        # Contract mode (ADR 0015): a goal-scoped, spec-frozen task-contract
        # registration PR. It uses the generalized deny-by-default validator and
        # forces the Guardian gate from the Goal's frozen-plan risk BEFORE any
        # implementation is dispatched. The Guardian gate here is INDEPENDENT of a
        # CCA verdict: a Contract PR carries no implementation for CCA to judge, so
        # requiring a verdict to reach the gate (as the single-task flow does)
        # would let a high-risk contract merge unapproved when CCA is absent.
        files_status = parse_changed_files_status(
            Path(args.changed_files_status), changed_files(Path(args.changed_files))
        )
        enforce_contract(
            target=target,
            goal_id=resolved_goal_id or "",
            changed_files_status=files_status,
            blocking=blocking,
            warnings=warnings,
            # The contract flow is the one path that carries a Goal's frozen plan
            # to the default branch (ADR 0015 plan-reachability gap); enable it.
            allow_source_plan_carry=True,
        )
        if resolved_goal_id:
            contract_risk = contract_guardian_risk(target, resolved_goal_id)
            if contract_risk not in _RISK_ORDER:
                # Fail closed: if the Guardian risk cannot be resolved from the
                # goal's frozen plan, the gate cannot be evaluated, so the contract
                # must not slip through as if it were unguarded. (enforce_contract
                # already blocks a risk-less goal registration; this guards the
                # guardian input independently in case the goal is on base.)
                blocking.append(
                    f"contract PR Guardian risk for goal {resolved_goal_id} could not be resolved "
                    f"from its frozen plan (got {contract_risk!r}); the Guardian gate cannot be evaluated"
                )
            else:
                enforce_guardian_policy(
                    pr=pr,
                    task={"risk_level": contract_risk},
                    target=target,
                    guardian_policy=args.guardian_policy,
                    guardian_comments=args.guardian_comments,
                    guardian_events=args.guardian_events,
                    guardian_timeline=args.guardian_timeline,
                    blocking=blocking,
                    warnings=warnings,
                    expected_repository=args.expected_repository,
                )
    elif post_merge_mode and pr:
        # post_merge_reconcile is a task-scoped reconcile of a merged task's
        # residual lock / status; a dedicated deny-by-default validator.
        files_status = parse_changed_files_status(
            Path(args.changed_files_status), changed_files(Path(args.changed_files))
        )
        enforce_post_merge_reconcile(
            target=target,
            task_id=resolved_task_id or "",
            base_shiki=Path(args.base_shiki) if args.base_shiki else None,
            changed_files_status=files_status,
            blocking=blocking,
            warnings=warnings,
            merged_pr_numbers=merged_prs,
            pr_body=body,
        )
    elif resolved_task_id:
        task = load_task(target, resolved_task_id)
        if task is None:
            blocking.append(f"Task file not found for {resolved_task_id}")
        else:
            task_goal_id = str(task.get("goal_id") or "")
            if resolved_goal_id and task_goal_id != resolved_goal_id:
                blocking.append(f"PR goal id {resolved_goal_id} does not match task goal_id {task_goal_id}")
            if not load_goal(target, task_goal_id):
                blocking.append(f"Goal file not found for {task_goal_id}")

            if pr:
                pr_number = pr.get("number")
                expected_pr = task.get("expected_pr")
                if expected_pr is not None and pr_number is not None and expected_pr != pr_number:
                    blocking.append(f"Task expected_pr {expected_pr} does not match PR #{pr_number}")
                elif expected_pr is None:
                    warnings.append(f"Task {resolved_task_id} has no expected_pr recorded")

                head_ref = pr.get("headRefName")
                expected_branch = task.get("expected_branch")
                if expected_branch and head_ref and expected_branch != head_ref:
                    blocking.append(f"Task expected_branch {expected_branch!r} does not match PR head {head_ref!r}")

            for dependency_id in task.get("dependencies") or []:
                dependency = load_task(target, str(dependency_id))
                if dependency is None:
                    blocking.append(f"Task dependency {dependency_id} has no task file")
                elif dependency.get("status") != "done":
                    blocking.append(f"Task dependency {dependency_id} is not done: {dependency.get('status')!r}")

            files = changed_files(Path(args.changed_files))
            files_status = parse_changed_files_status(Path(args.changed_files_status), files)
            locks = [str(lock) for lock in task.get("locks") or []]
            if files and not locks:
                blocking.append(f"Task {resolved_task_id} has no locks but PR changes files")
            # The task's declared (product) locks stay exactly what the plan froze;
            # the .shiki evidence the loop syncs onto the branch is covered by the
            # id-scoped mirror set derived from the task's own ids/ledgers at read
            # time. Union the two for the files-outside-locks gate only — lock
            # CONFLICT detection stays on the declared locks, since shared mirror
            # evidence is not a contended product resource.
            effective_locks = list(dict.fromkeys([*locks, *_derive_task_mirror_locks(target, task)]))
            for path in files_outside_locks(files, effective_locks):
                blocking.append(f"Changed file {path} is outside declared task locks")
            blocking.extend(active_lock_conflicts(target, resolved_task_id, locks, files))

            # ADR 0017: a proven bookkeeping closeout (task terminal state only, no
            # implementation) is evaluated as if low risk at the Guardian gate
            # below. Computed here (where the head task, base snapshot, diff and
            # merged-PR proof are all in hand); consulted only by
            # enforce_guardian_policy. Read-only — it never adds a blocking reason.
            bookkeeping_closeout = is_bookkeeping_closeout(
                target=target,
                task=task,
                task_id=resolved_task_id,
                goal_id=task_goal_id,
                base_shiki=Path(args.base_shiki) if args.base_shiki else None,
                changed_files_status=files_status,
                merged_pr_numbers=merged_prs,
            )

            ledger_entries = load_ledger_entries(target, task, warnings, blocking)
            if pr:
                enforce_untrusted_shiki_mutations(
                    target=target,
                    manifest=manifest,
                    base_shiki=Path(args.base_shiki) if args.base_shiki else None,
                    changed_files_status=files_status,
                    task=task,
                    goal_id=task_goal_id,
                    task_id=resolved_task_id,
                    pr=pr,
                    blocking=blocking,
                    warnings=warnings,
                )
            ledger_text = "\n".join(ledger_entry_text(entry) for entry in ledger_entries)
            for skill in task.get("required_skills") or []:
                skill_name = str(skill).strip().lower()
                if skill_name and skill_name != "none" and skill_name not in ledger_text:
                    blocking.append(f"Required skill {skill} has no matching ledger evidence")

    cca = load_json(Path(args.cca_verdict))
    if cca:
        for error in validate_cca_contract(target, cca):
            blocking.append(f"CCA verdict schema violation: {error}")
        if pr and args.cca_evidence_manifest:
            manifest_path = Path(args.cca_evidence_manifest)
            if not manifest_path.is_absolute():
                manifest_path = target / manifest_path
            evidence_dir = manifest_path.parent if manifest_path.parent != Path("") else Path(".shiki/gha")
            for error in validate_cca_evidence_contract(
                target=target,
                manifest_path=manifest_path,
                evidence_dir=evidence_dir,
                pr=pr,
                task_id=resolved_task_id,
                goal_id=resolved_goal_id,
                expected_repository=args.expected_repository,
            ):
                blocking.append(error)
        elif pr and args.expected_repository:
            blocking.append("CCA evidence manifest path is required when expected repository is provided")
        if cca.get("verdict") != "complete":
            blocking.append(f"CCA verdict is not complete: {cca.get('verdict')!r}")
        if resolved_task_id and cca.get("task_id") != resolved_task_id:
            blocking.append(f"CCA task_id {cca.get('task_id')!r} does not match PR task id {resolved_task_id}")
        if resolved_goal_id and cca.get("goal_id") != resolved_goal_id:
            blocking.append(f"CCA goal_id {cca.get('goal_id')!r} does not match PR goal id {resolved_goal_id}")
        if task and cca.get("goal_id") != task.get("goal_id"):
            blocking.append(f"CCA goal_id {cca.get('goal_id')!r} does not match task goal_id {task.get('goal_id')!r}")
        if pr:
            if cca.get("pr") != pr.get("number"):
                blocking.append(f"CCA pr {cca.get('pr')!r} does not match PR #{pr.get('number')}")
            if not cca.get("head_sha"):
                blocking.append("CCA head_sha is missing")
            elif pr.get("headRefOid") and cca.get("head_sha") != pr.get("headRefOid"):
                blocking.append("CCA head_sha does not match the current PR headRefOid")
        if cca.get("can_merge") is not True:
            warnings.append("CCA verdict did not set can_merge=true; MergeGate will rely on required checks and policy inputs")
        failures = blocking_checklist_failures(cca)
        if failures:
            blocking.append("CCA verdict contains blocking failed checklist items: " + ", ".join(failures))
        acceptance = cca.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance:
            blocking.append("CCA verdict acceptance evidence is empty")
        if task and task.get("status") not in {"review", "done"}:
            blocking.append(f"Task status must be review or done after CCA verdict; got {task.get('status')!r}")
        if pr and ledger_entries:
            pr_number = pr.get("number")
            pr_token = f"/pull/{pr_number}"
            pr_text = "\n".join(ledger_entry_text(entry) for entry in ledger_entries)
            if pr_number is not None and pr_token not in pr_text and f"pr #{pr_number}" not in pr_text:
                blocking.append(f"Task ledger evidence does not reference PR #{pr_number}")
        if pr:
            enforce_required_checks(pr, target, blocking, warnings)
            enforce_review_policy(pr, target, blocking)
            # Reconcile PRs carry no `task`, so the guardian gate would otherwise
            # miss the reconciled work's risk. Derive a risk-bearing task so a
            # high/critical merged task (post_merge) or goal (goal_reconcile)
            # still forces Guardian evaluation for its reconcile.
            guardian_task = task
            if guardian_task is None and post_merge_mode and resolved_task_id:
                guardian_task = load_task(target, resolved_task_id)
            elif guardian_task is None and reconcile_mode and resolved_goal_id:
                goal = load_goal(target, resolved_goal_id)
                if isinstance(goal, dict) and goal.get("risk_level"):
                    guardian_task = {"risk_level": goal.get("risk_level")}
            enforce_guardian_policy(
                pr=pr,
                task=guardian_task,
                target=target,
                guardian_policy=args.guardian_policy,
                guardian_comments=args.guardian_comments,
                guardian_events=args.guardian_events,
                guardian_timeline=args.guardian_timeline,
                blocking=blocking,
                warnings=warnings,
                expected_repository=args.expected_repository,
                bookkeeping_closeout=bookkeeping_closeout,
            )
    elif not args.allow_missing_cca:
        blocking.append(f"CCA verdict file not found at {args.cca_verdict}")

    changed_files_path = Path(args.changed_files)
    if changed_files_path.exists():
        files = changed_files(changed_files_path)
        if any(path.startswith(".github/workflows/") for path in files):
            warnings.append("workflow files changed; ci:infra or Guardian review may be required")
        if any("auth" in path.lower() or "secret" in path.lower() for path in files):
            warnings.append("auth/secret-like paths changed; ci:security or Guardian review may be required")

    result = {
        "mergegate": "blocked" if blocking else "ready",
        "task_id": resolved_task_id,
        "goal_id": resolved_goal_id,
        "blocking_reasons": blocking,
        "warnings": warnings,
    }
    write_json(Path(args.result_file), result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
