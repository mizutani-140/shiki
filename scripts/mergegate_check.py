#!/usr/bin/env python3
"""Deterministic MergeGate policy check for Shiki PRs."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import re
import shutil
import subprocess
import sys
import tempfile
import types
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


# An Amendment PR (Spec Amendment, ADR 0009/0015) declares itself with this exact
# HTML-comment marker. It carries an operator-approved amendment of a spec-frozen
# Goal's source plan — an append to ``spec_freeze.amendments`` — plus the task
# files bound to the newly amended plan, so a contract that implementation proved
# wrong (an unsatisfiable acceptance criterion, locks too narrow for the change
# the task requires) can be corrected on a protected branch instead of by a hand
# commit to main. Like contract/goal_reconcile, the marker only DECLARES intent;
# the maintainer-applied label below is the independent second factor.
AMENDMENT_MARKER = re.compile(r"<!--\s*shiki:amendment\s*-->")
# The amendment mode's second factor. Only a write-access maintainer/Guardian can
# apply it, so amendment mode cannot be self-granted from the PR body alone.
AMENDMENT_LABEL = "mergegate:amendment"

# Fenced code blocks and inline code spans in Markdown render their contents as
# literal text, not markup, so a marker QUOTED inside them (```` ``` ````…/`` ` ``…)
# is documentation, not a directive. This matters concretely: the amendment task's
# own scope documents the marker as `` `<!-- shiki:amendment -->` ``, and the goal
# loop inlines that scope verbatim into every implementation PR body it opens for
# the task (``github_pr_body``). Stripping code spans before matching keeps such a
# reproduced marker from self-declaring amendment mode. A real directive is a bare
# HTML comment and is never backtick-wrapped, so it survives the strip.
_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`+[^`]*`+")


def _strip_markdown_code(text: str) -> str:
    """Remove fenced code blocks and inline code spans from Markdown text."""
    return _INLINE_CODE.sub(" ", _FENCED_CODE.sub(" ", text))


def amendment_decision(pr: dict[str, Any]) -> tuple[bool, str | None]:
    """Decide whether a PR runs in amendment mode (marker + label).

    Requires BOTH the body marker (declares intent) AND the maintainer-applied
    label (authorizes the mode) — the same two-factor shape ``contract_decision``
    implements, with the same message when the label is missing. A marker without
    the label fails closed so untrusted PR text cannot self-grant the relaxed
    amendment mode.

    The marker is matched only OUTSIDE code spans: a marker quoted as literal
    text (e.g. a task scope that documents it, reproduced verbatim into the PR
    body by the goal loop) is not a directive and must not enter the mode.
    """
    body = str(pr.get("body") or "")
    marker = bool(AMENDMENT_MARKER.search(_strip_markdown_code(body)))
    label = AMENDMENT_LABEL in pr_label_names(pr)
    if marker and not label:
        return False, (
            f"amendment mode requires the {AMENDMENT_LABEL} label (a maintainer-applied "
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


def _check_frozen_dag_in_diff(
    target: Path, goal_id: str, frozen_titles: set[str], path: str, blocking: list[str], mode: str
) -> None:
    """Per-node validation of a goal's DAG file present in the diff.

    Every node must resolve to a frozen-plan task ANCHORED TO THIS GOAL (existing
    or added/modified in this PR), so a foreign task with a colliding title cannot
    be wired in (poisoning), and the DAG must COVER the full frozen plan — a
    truncated DAG that drops frozen tasks is rejected. Shared by the frozen
    registration modes (contract / goal_reconcile) and amendment mode, so the DAG
    rules are literally identical across them."""
    dag = load_json(target / path)
    nodes = dag.get("nodes") if isinstance(dag, dict) else None
    if not isinstance(nodes, list):
        blocking.append(f"{mode} DAG {path} must have a nodes list")
        return
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
    missing = frozen_titles - node_titles
    if missing:
        blocking.append(
            f"{mode} DAG must cover every frozen plan task; missing {sorted(missing)}"
        )


def _check_frozen_dag_head_invariant(
    target: Path, goal_id: str, frozen_tasks: dict[str, dict[str, Any]], blocking: list[str], mode: str
) -> None:
    """HEAD invariant + frozen-dependency edge binding for a goal's DAG.

    Independent of whether the DAG file is in this diff: after the registration or
    amendment, the goal's DAG at HEAD must cover every frozen-plan task, and its
    edges (plus each registered task's own ``dependencies`` field) must be EXACTLY
    the frozen plan's title-declared dependencies mapped to registered task ids.
    Shared by the frozen registration modes and amendment mode so the ordering
    semantics the DAG encodes cannot be dropped or invented by either."""
    frozen_titles = set(frozen_tasks)
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
    # dependent). Otherwise a registration/amendment could register all
    # tasks/nodes but drop or invent dependency edges, weakening the
    # dependency-done ordering the DAG encodes. Only checked once every frozen task
    # is covered (ids resolvable).
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
            # DAG restore is allowed; every node must resolve to a frozen-plan task
            # anchored to this goal, and the DAG must cover the full frozen plan (a
            # truncated DAG that drops frozen tasks would later let validate force
            # premature goal-complete on the registered subset).
            _check_frozen_dag_in_diff(target, goal_id, frozen_titles, path, blocking, mode)
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

    # HEAD invariant + frozen-dependency edge binding (independent of whether the
    # DAG file is in this diff): after the reconcile, the goal's DAG must cover
    # every frozen-plan task and its edges/task.dependencies must match the frozen
    # plan. Without this, a PR could register a subset of tasks and simply OMIT the
    # DAG; a legacy DAG-less goal would then let validate force premature
    # goal-complete on the registered subset, abandoning the unregistered frozen
    # tasks. This makes "the reconcile restores the goal's DAG" a hard invariant.
    _check_frozen_dag_head_invariant(target, goal_id, frozen_tasks, blocking, mode)


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


# A task file an amendment MODIFIES freezes these fields to the base snapshot: they
# are the loop-owned bookkeeping (expected_pr/closeout_pr/expected_branch/
# ledger_evidence) that an amendment — which re-opens a task for re-dispatch, not
# a completion — must never rewrite. This is the ADR 0017 mutable set minus
# ``status`` (which the amendment MAY move, but only to planned/ready below).
_AMENDMENT_TASK_FROZEN_FIELDS: tuple[str, ...] = (
    "expected_pr",
    "closeout_pr",
    "expected_branch",
    "ledger_evidence",
)
# An amended task may only be re-opened to a pre-dispatch status; a move to any
# terminal or in-flight status (done/review/cancelled/...) is not an amendment.
_AMENDMENT_TASK_STATUSES = {"planned", "ready"}


def _validate_amendment_source_plan(
    *,
    target: Path,
    base_shiki: Path | None,
    source_plan_id: str,
    entry: ChangedFile,
    blocking: list[str],
) -> None:
    """Validate the goal's own source plan an Amendment PR MODIFIES.

    Deny by default. The only permitted change is an APPEND to
    ``spec_freeze.amendments``: the head's ``spec_freeze`` must equal the base's on
    every key EXCEPT ``amendments``, the base's ``amendments`` must be a strict
    PREFIX of the head's, and there must be at least one new entry. The plan's id
    must not change, and a plan that is not the goal's ``source_plan`` or is not
    spec-frozen at base is blocked — so an amendment can never author a fresh
    spec_freeze, freeze a draft plan, edit/remove/reorder an existing amendment, or
    amend a plan other than the one that governs this goal."""
    path = normalize_repo_path(entry.path)
    plan_id = Path(path).stem
    if not source_plan_id or plan_id != source_plan_id:
        blocking.append(
            f"amendment may only amend the goal's source_plan {source_plan_id or '<unresolved>'}, not {plan_id}"
        )
        return
    if entry.status != "M":
        blocking.append(
            f"amendment may only MODIFY the goal's source plan, not {entry.status} {path}"
        )
        return
    if base_shiki is None or not base_shiki.exists():
        blocking.append(
            f"amendment requires a base snapshot to verify the source plan amendment {path}"
        )
        return
    head_plan = load_json(target / path)
    base_plan = load_json(base_shiki / "plans" / f"{plan_id}.json")
    if not isinstance(head_plan, dict):
        blocking.append(f"amendment source plan {path} is not a JSON object")
        return
    if not isinstance(base_plan, dict):
        blocking.append(
            f"amendment source plan {path} has no spec-frozen base snapshot to amend against"
        )
        return
    if str(head_plan.get("id") or "") != plan_id:
        blocking.append(
            f"amendment source plan filename {path} does not match its id {head_plan.get('id')!r}"
        )
    if str(head_plan.get("id") or "") != str(base_plan.get("id") or ""):
        blocking.append(f"amendment must not change the id of source plan {path}")
    base_freeze = base_plan.get("spec_freeze")
    head_freeze = head_plan.get("spec_freeze")
    if not (isinstance(base_freeze, dict) and base_freeze.get("status") == "frozen"):
        blocking.append(f"amendment source plan {path} is not spec-frozen at base")
        return
    if not isinstance(head_freeze, dict):
        blocking.append(f"amendment source plan {path} must keep a spec_freeze block")
        return
    # Every spec_freeze key EXCEPT amendments is frozen to base: an amendment may
    # only append amendments, never re-author status/approved_by/prd/source.
    base_other = {k: v for k, v in base_freeze.items() if k != "amendments"}
    head_other = {k: v for k, v in head_freeze.items() if k != "amendments"}
    if head_other != base_other:
        blocking.append(
            f"amendment must not change spec_freeze fields other than amendments "
            f"(e.g. status/approved_by/prd/source) of {path}"
        )
    base_amendments = base_freeze.get("amendments") or []
    head_amendments = head_freeze.get("amendments") or []
    if not isinstance(base_amendments, list) or not isinstance(head_amendments, list):
        blocking.append(f"amendment spec_freeze.amendments of {path} must be a list")
        return
    # The base's amendments must remain a strict PREFIX of the head's: editing,
    # removing, or reordering an existing entry breaks the prefix and is blocked.
    if head_amendments[: len(base_amendments)] != base_amendments:
        blocking.append(
            f"amendment must not edit, remove, or reorder existing spec_freeze.amendments entries of {path}; "
            "the base must remain a strict prefix of the head"
        )
    elif len(head_amendments) <= len(base_amendments):
        blocking.append(
            f"amendment must append at least one new spec_freeze.amendments entry to {path}"
        )


def _validate_amendment_task(
    *,
    target: Path,
    base_shiki: Path | None,
    goal_id: str,
    frozen_tasks: dict[str, dict[str, Any]],
    frozen_titles: set[str],
    entry: ChangedFile,
    blocking: list[str],
) -> None:
    """Validate a task file an Amendment PR MODIFIES.

    Deny by default. The task must belong to THIS goal, must be a MODIFY (an ADDed
    task file is blocked — new tasks come through contract mode), must match the
    amended (HEAD) plan's frozen definition for its title via the existing
    ``_frozen_task_match_errors`` binding, may move ``status`` only to planned or
    ready, and must keep ``expected_pr``/``closeout_pr``/``expected_branch``/
    ``ledger_evidence`` byte-identical to the base snapshot."""
    path = normalize_repo_path(entry.path)
    task_id = Path(path).stem
    if entry.status != "M":
        # The only non-MODIFY status reaching here is an ADD (deletes are filtered
        # by the caller). A brand-new task file comes through contract mode, never
        # an amendment: an amendment re-opens EXISTING tasks bound to the base.
        blocking.append(
            f"amendment may only MODIFY this goal's existing task files, not ADD {path}"
        )
        return
    data = load_json(target / path)
    if not isinstance(data, dict):
        blocking.append(f"amendment task file {path} is not a JSON object")
        return
    if str(data.get("id") or "") != task_id:
        blocking.append(f"amendment task filename {path} does not match its id {data.get('id')!r}")
    if str(data.get("goal_id") or "") != goal_id:
        blocking.append(f"amendment task {task_id} is not anchored to goal {goal_id}")
        return
    title = str(data.get("title") or "").strip()
    if title not in frozen_titles:
        blocking.append(f"amendment task {task_id} title {title!r} is not in the goal's frozen plan")
    else:
        # Bind to the AMENDED plan's frozen task definition: after the amendment the
        # task must match every governance field the amended plan declares (this is
        # what lets a widened lock set pass — plan and task widen together).
        blocking.extend(_frozen_task_match_errors(task_id, data, frozen_tasks[title], "amendment"))
    if data.get("status") not in _AMENDMENT_TASK_STATUSES:
        blocking.append(
            f"amendment task {task_id} status must be planned or ready, not {data.get('status')!r}"
        )
    if base_shiki is None or not base_shiki.exists():
        blocking.append(
            f"amendment requires a base snapshot to verify the frozen fields of task {task_id}"
        )
        return
    base_task = load_json(base_shiki / "tasks" / f"{task_id}.json")
    if not isinstance(base_task, dict):
        blocking.append(
            f"amendment task {task_id} has no base snapshot to verify its frozen fields against"
        )
        return
    for field in _AMENDMENT_TASK_FROZEN_FIELDS:
        if data.get(field) != base_task.get(field):
            blocking.append(
                f"amendment must not change frozen field {field!r} of task {task_id} (frozen to the base snapshot)"
            )


def enforce_amendment(
    *,
    target: Path,
    goal_id: str,
    base_shiki: Path | None,
    changed_files_status: list[ChangedFile],
    blocking: list[str],
    warnings: list[str],
) -> None:
    """Validate a Spec Amendment PR (ADR 0009/0015): correct a spec-frozen Goal's
    contract on a protected branch. Deny by default.

    An amendment PR may change EXACTLY four classes of file, everything else is
    blocked with the mode named:

      1. the goal's own ``source_plan`` (MODIFY) — an append to
         ``spec_freeze.amendments`` only (see ``_validate_amendment_source_plan``);
      2. task files of THIS goal (MODIFY) — each bound to the amended plan's frozen
         definition, ``status`` movable only to planned/ready, and
         expected_pr/closeout_pr/expected_branch/ledger_evidence frozen to base
         (see ``_validate_amendment_task``);
      3. the goal's DAG — under the same rules the frozen-registration modes apply
         (``_check_frozen_dag_in_diff`` + ``_check_frozen_dag_head_invariant``);
      4. a goal-scoped ledger entry of type ``contract-amended``, which is REQUIRED
         (mirroring contract mode's ``task-registered`` requirement).

    The frozen authority is the HEAD (amended) plan — ``_frozen_plan_tasks`` reads
    the goal's source_plan at HEAD, which spec_freeze keeps frozen through the
    amendment. Guardian evaluation is forced separately in ``main`` from the
    stronger of the goal's base-plan and head-plan risk via ``contract_guardian_risk``.
    """
    if not goal_id:
        blocking.append("amendment PR must reference a Shiki goal id")
        return
    if base_shiki is None or not base_shiki.exists():
        blocking.append(
            "amendment requires a base .shiki snapshot to verify the plan amendment and frozen task fields"
        )
        return
    frozen_tasks, frozen_errors = _frozen_plan_tasks(target, goal_id, "amendment")
    if frozen_errors:
        blocking.extend(frozen_errors)
        return
    frozen_titles = set(frozen_tasks)

    goal_for_plan = load_goal(target, goal_id)
    source_plan_id = (
        str(goal_for_plan.get("source_plan") or "") if isinstance(goal_for_plan, dict) else ""
    )
    dag_file = f".shiki/dag/{goal_id}.json"
    amendment_ledger_seen = False

    for entry in changed_files_status:
        path = normalize_repo_path(entry.path)
        if not path.startswith(".shiki/"):
            blocking.append(f"amendment must not change non-Shiki (implementation) file {path}")
            continue
        if entry.status == "D":
            blocking.append(f"amendment must not delete {path}")
            continue
        if path.startswith(".shiki/plans/") and path.endswith(".json"):
            _validate_amendment_source_plan(
                target=target,
                base_shiki=base_shiki,
                source_plan_id=source_plan_id,
                entry=entry,
                blocking=blocking,
            )
        elif path.startswith(".shiki/tasks/") and path.endswith(".json"):
            _validate_amendment_task(
                target=target,
                base_shiki=base_shiki,
                goal_id=goal_id,
                frozen_tasks=frozen_tasks,
                frozen_titles=frozen_titles,
                entry=entry,
                blocking=blocking,
            )
        elif path == dag_file:
            _check_frozen_dag_in_diff(target, goal_id, frozen_titles, path, blocking, "amendment")
        elif path.startswith(".shiki/dag/") and path.endswith(".json"):
            blocking.append(f"amendment must not change another goal's DAG {path}")
        elif path.startswith(".shiki/ledger/") and path.endswith(".json"):
            if entry.status != "A":
                blocking.append(f"amendment must append, not modify, ledger {path}")
                continue
            led = load_json(target / path)
            if not isinstance(led, dict) or str(led.get("goal_id") or "") != goal_id:
                blocking.append(f"amendment ledger {path} must be scoped to goal {goal_id}")
                continue
            # The amendment event must be a contract-amended ledger, not any
            # goal-scoped ledger, so the requirement proves an actual amendment.
            if led.get("type") == "contract-amended":
                amendment_ledger_seen = True
        else:
            blocking.append(
                f"amendment must not change {path}; only the goal's source_plan amendment, "
                f"this goal's task files, the DAG for {goal_id}, and a contract-amended ledger are allowed"
            )

    if not amendment_ledger_seen:
        blocking.append(
            f"amendment must include a goal-scoped contract-amended ledger event for {goal_id}"
        )

    # HEAD invariant + frozen-dependency edge binding: the amended DAG must still
    # cover every frozen (amended) plan task and match its dependency edges. Runs
    # whether or not the DAG file is in this diff (an amendment that only widens a
    # task's locks need not touch the DAG, but one that adds a plan task must).
    _check_frozen_dag_head_invariant(target, goal_id, frozen_tasks, blocking, "amendment")


# --- Migration mode (repo-wide mirror migration; a re-run proof) -------------
#
# A Migration PR delivers a change that must touch MANY mirror records at once — a
# repository-wide backfill that no goal-shaped mode can carry, because contract,
# goal_reconcile and amendment are each scoped to a SINGLE goal's records and
# reject a foreign task/goal file independently of locks (a repository-wide change
# has no goal). Migration mode's entire proof obligation is that the PR's .shiki
# diff EQUALS the output of re-running a migration REGISTERED ON THE BASE BRANCH:
# apply the declared migration to a copy of the base .shiki snapshot and require the
# result to equal the PR head's .shiki, file for file and byte for byte.
#
# Like contract/amendment it is two-factor: the body marker DECLARES intent and the
# maintainer-applied label AUTHORIZES the mode (same message as contract when the
# label is missing). A DIRECTIVE marker is a bare HTML comment on its OWN LINE — the
# way an operator (and the adversarial fixtures) place it. Two precautions keep a
# marker that is merely DESCRIBED from self-declaring the mode: the match runs after
# code spans are stripped (as amendment_decision does), and it is anchored to a full
# line. Both are load-bearing here, because the goal loop inlines this task's OWN
# scope AND acceptance_checks verbatim into the implementation PR body it opens
# (shiki_github.github_pr_body) — and those enumerate the marker in prose. Line
# anchoring is what stops the bare, mid-sentence mention in an acceptance criterion
# from blocking this very task's PR on the missing label.
MIGRATION_MARKER = re.compile(r"^[ \t]*<!--\s*shiki:migration\s*-->[ \t]*$", re.MULTILINE)
MIGRATION_LABEL = "mergegate:migration"
# The migration id the PR declares, on its OWN line: ``Migration: <id>``. Anchored
# to the start of a line so a mention mid-sentence (or a backtick-wrapped example
# in reproduced task scope, which never begins a line) is not read as a directive.
_MIGRATION_ID_LINE = re.compile(r"^[ \t]*Migration:[ \t]*(\S+)[ \t]*$", re.MULTILINE)


def migration_decision(pr: dict[str, Any]) -> tuple[bool, str | None]:
    """Decide whether a PR runs in migration mode (marker + label).

    The same two-factor shape ``contract_decision`` implements, with the same
    message when the label is missing: a marker without the maintainer-applied
    label fails closed so untrusted PR text cannot self-grant the mode. The marker
    is matched only outside code spans (as ``amendment_decision`` does) so a quoted
    marker in reproduced task scope is documentation, not a declaration.
    """
    body = str(pr.get("body") or "")
    marker = bool(MIGRATION_MARKER.search(_strip_markdown_code(body)))
    label = MIGRATION_LABEL in pr_label_names(pr)
    if marker and not label:
        return False, (
            f"migration mode requires the {MIGRATION_LABEL} label (a maintainer-applied "
            "second factor) in addition to the body marker"
        )
    return (marker and label), None


def declared_migration_ids(body: str) -> list[str]:
    """Every id declared on its own ``Migration: <id>`` line in the PR body."""
    return [match.group(1) for match in _MIGRATION_ID_LINE.finditer(body)]


def _git_show_base(target: Path, base_ref: str, repo_path: str) -> str:
    """Read ``repo_path`` from the base branch via a read-only ``git show``.

    Tries ``origin/<base_ref>`` first — the MergeGate workflow fetches it with
    ``fetch-depth: 0`` (shiki-mergegate.yml) — then the bare ``<base_ref>`` as a
    fallback for a local base branch. Raises ``FileNotFoundError`` naming every
    ref it tried when neither resolves the path."""
    errors: list[str] = []
    for ref in (f"origin/{base_ref}", base_ref):
        result = subprocess.run(
            ["git", "show", f"{ref}:{repo_path}"],
            cwd=str(target),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout
        errors.append(f"{ref}: {result.stderr.strip() or 'not found'}")
    raise FileNotFoundError(f"could not read {repo_path} from the base branch ({'; '.join(errors)})")


def resolve_base_migration_registry(target: Path, base_ref: str) -> tuple[Any, ...]:
    """Resolve the migration registry from the BASE branch's
    ``scripts/shiki_migrations.py`` — NEVER the PR head's.

    THIS IS THE SECURITY PROPERTY OF MIGRATION MODE. Do NOT simplify this to import
    the head registry (``from shiki_migrations import migration_registry``): the
    migration function whose output the PR is measured against MUST come from code
    the base branch already merged. If it came from the PR head, a PR could define a
    migration that emits exactly the diff it wants and the re-run comparison would
    pass — proving nothing. Reading from base means the gate executes trusted code
    the PR cannot alter.

    The base ``scripts/`` is deliberately read from git rather than a snapshot: the
    ``--base-shiki`` archive the workflow builds is ``.shiki``-only, and staging
    base ``scripts/`` would need a new workflow flag and archive step (out of this
    change's scope). So the base source is read with ``git show`` (using the fetch
    the MergeGate workflow already performs) and executed in an isolated namespace;
    its only external dependency (``shiki_process``) resolves from ``scripts/`` on
    ``sys.path``, infrastructure that carries no migration logic.
    """
    source = _git_show_base(target, base_ref, "scripts/shiki_migrations.py")
    # Execute the base source as a REAL module object registered in sys.modules:
    # dataclass field resolution (Python 3.12+) looks the class's module up there,
    # so a plain-dict namespace would fail on the module's ``@dataclass Migration``.
    # ``shiki_process`` — the module's only external dependency — resolves from
    # ``scripts/`` on sys.path, infrastructure that carries no migration logic.
    module = types.ModuleType("shiki_migrations_base")
    module.__file__ = "<base>/scripts/shiki_migrations.py"
    sys.modules[module.__name__] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return tuple(module.migration_registry())


def _read_shiki_tree(shiki_dir: Path) -> dict[str, bytes]:
    """Every file under a ``.shiki`` directory, keyed by its repo-relative
    (``.shiki/...``) path and mapped to its raw bytes. Order-independent."""
    files: dict[str, bytes] = {}
    if not shiki_dir.exists():
        return files
    for path in shiki_dir.rglob("*"):
        if path.is_file():
            rel = ".shiki/" + path.relative_to(shiki_dir).as_posix()
            files[rel] = path.read_bytes()
    return files


def _base_applied_migration_ids(base_shiki: Path) -> set[str]:
    """Migration ids recorded ``applied`` in the base ``.shiki/migrations/state.json``.
    A missing/unreadable/malformed state file yields the empty set (nothing proven
    applied), so the already-applied block never fires on absent state."""
    try:
        state = load_json(base_shiki / "migrations" / "state.json")
    except (OSError, ValueError):
        return set()
    if not isinstance(state, dict) or not isinstance(state.get("applied"), list):
        return set()
    return {
        str(record.get("id"))
        for record in state["applied"]
        if isinstance(record, dict) and record.get("id")
    }


def enforce_migration(
    *,
    target: Path,
    pr: dict[str, Any],
    base_shiki: Path | None,
    changed_files_status: list[ChangedFile],
    blocking: list[str],
    warnings: list[str],
    base_registry: tuple[Any, ...] | None = None,
) -> None:
    """Validate a Migration PR: the .shiki diff must EQUAL the re-run output of a
    BASE-registered migration. Deny by default.

    ``base_registry`` is injected only by tests to exercise the comparison and
    precondition logic directly; in production it is None and the registry is
    resolved from the base branch via ``resolve_base_migration_registry`` (the
    security property — the migration function is read from base, never the PR
    head). Every other input — the base snapshot applied against, the already-applied
    state, the base registry — is reconstructed from the base branch the PR head
    cannot forge; the PR head is only ever the thing COMPARED.
    """
    if base_shiki is None or not base_shiki.exists():
        blocking.append("migration mode requires a base .shiki snapshot to re-run the declared migration")
        return

    body = str(pr.get("body") or "")
    ids = declared_migration_ids(body)
    if not ids:
        blocking.append("migration PR body must declare the migration id on its own line as 'Migration: <id>'")
        return
    if len(ids) > 1:
        blocking.append(f"migration PR body must declare exactly one 'Migration:' line; found {sorted(ids)}")
        return
    migration_id = ids[0]

    if base_registry is None:
        base_ref = str(pr.get("baseRefName") or "")
        if not base_ref:
            blocking.append("migration mode requires the PR baseRefName to resolve the base branch migration registry")
            return
        try:
            base_registry = resolve_base_migration_registry(target, base_ref)
        except Exception as error:  # noqa: BLE001 - resolving base code can fail many ways; fail closed with a named reason, never crash the gate
            blocking.append(f"migration mode could not resolve the base branch migration registry: {error}")
            return

    by_id = {str(migration.id): migration for migration in base_registry}
    migration = by_id.get(migration_id)
    if migration is None:
        blocking.append(
            f"migration {migration_id} is not registered in the base branch migration registry; "
            "a PR cannot define the migration whose output it is measured against"
        )
        return
    if getattr(migration, "destructive", False):
        blocking.append(f"migration {migration_id} is destructive; migration mode refuses destructive migrations")
        return
    if migration_id in _base_applied_migration_ids(base_shiki):
        blocking.append(
            f"migration {migration_id} is already recorded as applied in the base .shiki/migrations/state.json"
        )
        return

    # Re-run the BASE migration against a COPY of the base snapshot. Both the input
    # (the base .shiki tree) and the code (the base migration function) come from the
    # base branch; the PR head influences neither, so it can only ever match or fail
    # to match the deterministic output.
    base_files = _read_shiki_tree(base_shiki)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            rerun_root = Path(tmp)
            shutil.copytree(base_shiki, rerun_root / ".shiki")
            apply = getattr(migration, "apply", None)
            if apply is not None:
                apply(rerun_root, False)
            rerun_files = _read_shiki_tree(rerun_root / ".shiki")
    except Exception as error:  # noqa: BLE001 - the base migration runs arbitrary (trusted) code; any failure is a named block, never a gate crash
        blocking.append(f"re-running migration {migration_id} against the base snapshot failed: {error}")
        return

    # The change set the re-run PRODUCES: paths whose bytes differ from base, mapped
    # to the new bytes (None means the migration deleted the file).
    rerun_changes: dict[str, bytes | None] = {}
    for rel in set(base_files) | set(rerun_files):
        if base_files.get(rel) != rerun_files.get(rel):
            rerun_changes[rel] = rerun_files.get(rel)

    # The change set the PR DELIVERS, read from the diff (head bytes from the working
    # tree; None means the PR deleted the file). A non-.shiki change is its own
    # blocking reason and is excluded here so it is not ALSO reported as unproduced.
    head_changes: dict[str, bytes | None] = {}
    for entry in changed_files_status:
        path = normalize_repo_path(entry.path)
        if not path.startswith(".shiki/"):
            blocking.append(f"migration must not change file outside .shiki/: {path}")
            continue
        head_changes[path] = None if entry.status == "D" else file_bytes(target / path)

    # Require the two change sets to be identical, path for path and byte for byte.
    # Each mismatch shape is a distinct blocking reason naming the path.
    for rel in sorted(set(head_changes) | set(rerun_changes)):
        in_head = rel in head_changes
        in_rerun = rel in rerun_changes
        if in_head and not in_rerun:
            blocking.append(
                f"migration PR changes {rel}, which re-running migration {migration_id} does not produce"
            )
        elif in_rerun and not in_head:
            blocking.append(
                f"migration re-run produces {rel}, but the PR head does not include it"
            )
        elif head_changes[rel] != rerun_changes[rel]:
            if head_changes[rel] is None or rerun_changes[rel] is None:
                blocking.append(
                    f"migration output for {rel} does not match the PR head (present in one, absent in the other)"
                )
            else:
                blocking.append(
                    f"migration output for {rel} does not match the re-run byte-for-byte"
                )


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


def failing_acceptance_criteria(verdict: dict[str, Any]) -> list[str]:
    """Acceptance criteria whose status is ``fail`` or ``insufficient_evidence``.

    Every acceptance criterion is blocking (there is no ``blocking`` flag on
    acceptance entries), so a ``complete`` verdict may not carry a criterion
    that durable evidence shows failed (``fail``) or that lacks proof
    (``insufficient_evidence``): either contradicts the ``complete`` judgment.
    Each offender is named so MergeGate can point at the specific criterion.
    """
    failures: list[str] = []
    for item in verdict.get("acceptance") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status in {"fail", "insufficient_evidence"}:
            failures.append(str(item.get("criterion") or "<unknown>"))
    return failures


# A checklist item is judged to a TERMINAL status when its work is resolved:
# ``pass`` (proven satisfied), ``fail`` (proven missing/incorrect), or
# ``not_applicable`` (justified exclusion). ``insufficient_evidence`` is NOT
# terminal — it means proof is still missing, so the item was not actually
# resolved. A task's ``cca_checklist_profile`` id must appear in the verdict
# checklist judged to one of these terminal statuses; absent, or present only at
# ``insufficient_evidence``, means the required item was silently dropped or left
# unresolved.
TERMINAL_CHECKLIST_STATUSES = {"pass", "fail", "not_applicable"}


def checklist_profile_coverage_failures(task: dict[str, Any] | None, verdict: dict[str, Any]) -> list[str]:
    """Profile ids the verdict fails to cover with a terminal checklist status.

    Every id in the task's ``cca_checklist_profile`` must appear in the verdict
    checklist judged to a terminal status (see ``TERMINAL_CHECKLIST_STATUSES``).
    An id that is absent, or present only at ``insufficient_evidence``, is
    reported so MergeGate blocks — a verdict cannot claim completion while
    silently omitting a required checklist item or leaving it unresolved. A task
    with no (or an empty) profile imposes no requirement and yields no reasons.
    """
    if not isinstance(task, dict):
        return []
    profile = task.get("cca_checklist_profile")
    if not isinstance(profile, list) or not profile:
        return []
    statuses: dict[str, str] = {}
    for item in verdict.get("checklist") or []:
        if isinstance(item, dict) and item.get("id") is not None:
            statuses[str(item.get("id"))] = str(item.get("status") or "").strip().lower()
    reasons: list[str] = []
    for raw_id in profile:
        profile_id = str(raw_id)
        status = statuses.get(profile_id)
        if status is None:
            reasons.append(
                f"CCA checklist profile id {profile_id} is missing from the verdict checklist"
            )
        elif status not in TERMINAL_CHECKLIST_STATUSES:
            reasons.append(
                f"CCA checklist profile id {profile_id} has non-terminal status {status!r} in the verdict checklist"
            )
    return reasons


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
#
# ``expected_branch`` belongs here — it is NOT a governance field. The goal loop
# cuts the closeout onto ``shiki/<task_id>-closeout`` and rewrites the closeout
# task file's ``expected_branch`` to that new branch on EVERY closeout by
# construction (scripts/shiki_loop.py), because MergeGate independently requires a
# PR's task ``expected_branch`` to equal the PR's actual head ref: the metadata gate
# in ``main`` blocks when ``expected_branch != headRefName``.
# So every real closeout differs from its base on ``expected_branch``, and without
# this entry condition 4 would reject every closeout before any other condition is
# reached — the exact defect ADR 0017 exists to remove. That independent head-ref
# binding is load-bearing for THIS whitelist entry: a PR cannot set
# ``expected_branch`` to a branch it is not on, so admitting the field here cannot
# let a closeout impersonate another branch. Do not weaken that binding.
_CLOSEOUT_MUTABLE_TASK_FIELDS = frozenset({"status", "expected_pr", "closeout_pr", "ledger_evidence", "expected_branch"})


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
         — only the ``_CLOSEOUT_MUTABLE_TASK_FIELDS`` (status, expected_pr,
         closeout_pr, ledger_evidence and expected_branch — see that definition for
         why the branch rewrite is safe) may differ;
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
    contract_approval: Any = None,
    base_sync_carry: bool = False,
    default_branch: str = "",
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

    # ADR 0015 Contract Approval OR-branch ("Evaluation placement"). The Guardian
    # requirement is satisfied by EITHER a live PR approval (the
    # ``evaluate_guardian_approval`` path below) OR a proven Contract Approval: a
    # normal implementation PR whose task contract was registered to the default
    # branch and Guardian-approved BEFORE dispatch. ``evaluate_guardian_approval``
    # is never consulted or modified here, so a defect in the contract path
    # degrades to "does not apply" and the live-approval path below runs
    # byte-for-byte as today. When the carry applies, live-approval evidence is
    # not required at all — the approval was recorded on the merged Contract PR.
    if contract_approval is not None and getattr(contract_approval, "applies", False):
        warnings.append(
            "Guardian approval satisfied by "
            + ", ".join(contract_approval.sources)
            + " (ADR 0015 Contract Approval: the task contract was registered and Guardian-approved before dispatch)"
        )
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
    # The carry evaluates the TASK's resolved risk (a proven bookkeeping closeout
    # is treated as low here, identically to _guardian_risk_labels), never the PR
    # labels — a label escalation is refused inside the evaluator. The git proof
    # runs in-process against ``target`` and is never persisted.
    carry_resolved_risk = "low" if bookkeeping_closeout else str((task or {}).get("risk_level") or "")
    result = evaluate_guardian_approval(
        policy=policy,
        pr=pr,
        reviews=reviews,
        comments=comments,
        label_events=events + timeline,
        head_sha=head_sha,
        expected_repo=expected_repository,
        base_sync_carry=base_sync_carry,
        default_branch=default_branch,
        carry_target=target,
        resolved_risk=carry_resolved_risk,
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


def lock_scope_paths(changed_files_status: list[ChangedFile]) -> list[str]:
    """The path set the lock-scope gates must see across the WHOLE diff.

    ``parse_changed_files_status`` decomposes a rename into two entries —
    ``D(old)`` and ``A(new)`` — so collecting each entry's path yields BOTH legs
    of every rename. git's name-only diff (``changed-files.txt``) carries only a
    rename's NEW path, so feeding it to ``files_outside_locks`` /
    ``active_lock_conflicts`` would let a rename move a file out of locked scope
    unseen. Deriving from ``files_status`` closes that gap. Order-stable,
    de-duplicated.
    """
    return list(dict.fromkeys(normalize_repo_path(entry.path) for entry in changed_files_status if entry.path))


def runtime_evidence_path(path: str) -> bool:
    """Whether a path is workflow-owned runtime evidence a PR must never author.

    The runtime filenames are matched EXACTLY (``cca-verdict.json`` /
    ``mergegate-result.json``), not by a ``startswith`` prefix: a prefix match
    also caught ``.shiki/schemas/cca-verdict.schema.json`` — a real repository
    contract — and made it permanently unmergeable. The whole ``.shiki/gha`` and
    ``.shiki/cca`` subtrees are still runtime evidence by location.
    """
    normalized = normalize_repo_path(path)
    name = Path(normalized).name
    return (
        normalized.startswith(".shiki/gha/")
        or normalized.startswith(".shiki/cca/")
        or (normalized.startswith(".shiki/") and name == "cca-verdict.json")
        or (normalized.startswith(".shiki/") and name == "mergegate-result.json")
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


# The .shiki mirror directories governed by the GENERIC mirror-class rule: every
# tracked mirror directory that lacks a dedicated per-record rule of its own.
# tasks/goals/locks/ledger/repairs are handled by their own branches above, and
# plans has the extra spec_freeze guard below, so all six are excluded here. For
# these the rule is uniform: a PR may never delete a record, and every added or
# modified record must be scoped to the PR's own task or goal. Without it, a PR
# with a broad lock (path:.shiki/**) could delete a foreign goal's DAG or rewrite
# another goal's report and files-outside-locks would wave it through.
_GENERIC_MIRROR_DIRS: tuple[str, ...] = (
    "dag",
    "reports",
    "memories",
    "runs",
    "worktrees",
    "handoffs",
    "runner",
    "smoke",
    "starts",
    "inbox",
)


def _tracked_mirror_subdirs(manifest: dict[str, Any]) -> list[str]:
    """The last path component of every tracked ``state_class: mirror`` directory
    in the manifest (e.g. ``.shiki/reports`` -> ``reports``). Drives the
    base-snapshot delete-protection loop so it covers every mirror directory, not
    only tasks/goals/locks. Order-stable.

    ``tasks``, ``goals`` and ``locks`` are a hardcoded floor: they were protected
    before this generalization, so they stay protected even when the manifest is
    missing/unreadable (``manifest`` empty) — never protecting LESS than before.
    """
    subdirs: list[str] = ["tasks", "goals", "locks"]
    directories = manifest.get("directories") if isinstance(manifest, dict) else None
    for raw_path, metadata in (directories or {}).items():
        if not isinstance(raw_path, str) or not isinstance(metadata, dict):
            continue
        if metadata.get("state_class") != "mirror" or metadata.get("tracked") is not True:
            continue
        normalized = normalize_repo_path(raw_path)
        if normalized.startswith(".shiki/"):
            subdirs.append(normalized[len(".shiki/") :])
    return list(dict.fromkeys(subdirs))


def _mirror_record_scoped(data: Any, path: str, *, task_id: str, goal_id: str) -> bool:
    """Whether a mirror record is scoped to the PR's own task or goal.

    A record is own-scoped when its filename stem is this task's or goal's id
    (id-named records such as ``dag/<goal>.json`` and ``worktrees/<task>.json``),
    or when its JSON content is task/goal scoped by the same rule the ledger gate
    uses (``ledger_entry_allowed_for_task``): its ``task_id`` equals this task, or
    it carries no ``task_id`` and its ``goal_id`` equals this goal. A record naming
    a DIFFERENT task — even one of the same goal — is NOT own-scoped
    (anti-padding). Unreadable/malformed content (``data`` not a dict) is
    fail-closed: not scoped.
    """
    stem = Path(path).stem
    if stem and (stem == task_id or stem == goal_id):
        return True
    if isinstance(data, dict):
        return ledger_entry_allowed_for_task(data, task_id=task_id, goal_id=goal_id)
    return False


# The governance contract a NORMAL task PR must not rewrite in its OWN task file
# (ADR 0015 Contract immutability, normal path). When the task file is present in
# the base snapshot the base is authority over these fields, so a head that
# differs on any of them is blocked. Every OTHER task field may legitimately move
# between the base registration and the implementation-PR head — the
# ``_CLOSEOUT_MUTABLE_TASK_FIELDS`` (status/expected_pr/closeout_pr/
# expected_branch/ledger_evidence) AND loop-written bookkeeping such as
# ``pre_pr_code_review`` / ``cca_rerun_count`` that ``shiki_loop`` writes onto the
# task file and syncs to the branch. Freezing the COMPLEMENT of the mutable set
# would therefore wrongly block real PRs; this is the explicit frozen set instead.
# It is DISJOINT from ``_CLOSEOUT_MUTABLE_TASK_FIELDS`` (bound by a consistency
# test) so the frozen contract and the may-move set cannot drift into overlap, and
# the mutable set stays the single ADR 0017 constant, never re-copied here.
_NORMAL_PATH_GOVERNANCE_FIELDS: tuple[str, ...] = (
    "scope",
    "non_goals",
    "required_skills",
    "risk_level",
    "locks",
    "acceptance_checks",
    "test_command",
)


def resolved_guardian_risk_never_weaker(
    base_task: dict[str, Any] | None, head_task: dict[str, Any]
) -> str:
    """The risk level the Guardian gate must use for a normal task PR: NEVER weaker
    than the base snapshot (ADR 0015). Returns ``max(base_risk, head_risk)`` by the
    canonical risk ordering, so a PR that lowered its own ``risk_level`` cannot
    dissolve its Guardian requirement — the gate still resolves at the base risk.
    With no base task (first registration, or no snapshot) the head risk stands."""
    head_risk = str(head_task.get("risk_level") or "")
    if not isinstance(base_task, dict):
        return head_risk
    return _max_risk(str(base_task.get("risk_level") or ""), head_risk)


def normal_task_contract_immutability_reasons(
    *,
    base_shiki: Path | None,
    task_id: str,
    head_task: dict[str, Any],
    changed_files_status: list[ChangedFile],
) -> list[str]:
    """Blocking reasons when a NORMAL task PR rewrites its OWN task file's frozen
    governance contract (ADR 0015 Contract immutability, normal path).

    Fires ONLY when the PR's own task file ``.shiki/tasks/<task_id>.json`` is added
    or modified in the diff — a delete is already blocked by
    ``enforce_untrusted_shiki_mutations`` and carries no contract to compare, and a
    PR that does not touch its own task file is unaffected. Resolution is
    base-when-present, exactly as ADR 0015 states:

      * base snapshot HAS the task file  -> the base is authority: every field in
        ``_NORMAL_PATH_GOVERNANCE_FIELDS`` must equal the base, and ``risk_level``
        may never resolve WEAKER than base (a standalone floor that holds even if
        the field-equality rule for ``risk_level`` is later relaxed);
      * base snapshot present WITHOUT the task file -> first registration (head is
        authority, which is how the goal loop registers tasks today) -> no reason;
      * no base snapshot available at all -> fail closed: a changed own task file
        with no base to check it against does not get a pass.

    Only ``main``'s normal single-task path (``elif resolved_task_id``) calls this.
    contract / goal_reconcile / post_merge_reconcile run their own validators in
    earlier branches and never reach here, so they are not double-reported; a
    bookkeeping closeout does reach here but keeps every governance field
    byte-identical to base, so it yields no reason and behaves exactly as before.
    """
    task_file = f".shiki/tasks/{task_id}.json"
    own_task_changed = any(
        normalize_repo_path(entry.path) == task_file and entry.status != "D"
        for entry in changed_files_status
    )
    if not own_task_changed:
        return []
    if base_shiki is None or not base_shiki.exists():
        # The comparison is required (the own task file changed) but there is no
        # base to check it against: fail closed rather than pass an unverifiable
        # contract change.
        return [
            f"PR changes its own task file {task_file} but no base .shiki snapshot is available to verify "
            "its frozen governance contract (ADR 0015 Contract immutability); failing closed"
        ]
    base_task_path = base_shiki / "tasks" / f"{task_id}.json"
    try:
        base_task = load_json(base_task_path)
    except (OSError, ValueError):
        # An unreadable/malformed base task cannot be compared against: fail closed,
        # never pass an unverifiable contract change (the base is committed main
        # state, so this is a defensive backstop, not an expected path).
        return [
            f"PR changes its own task file {task_file} but its base snapshot {base_task_path.name} could not be "
            "read to verify the frozen governance contract (ADR 0015 Contract immutability); failing closed"
        ]
    if not isinstance(base_task, dict):
        # First registration: the task is not on the base branch (base file absent
        # or not an object), so head is authority and the PR passes — this is how
        # the goal loop registers tasks today.
        return []
    reasons: list[str] = []
    for field in _NORMAL_PATH_GOVERNANCE_FIELDS:
        # Exclude the mutable set defensively: a field the closeout legitimately
        # moves must never be frozen here. Today the two sets are disjoint (a
        # consistency test binds them), so this only future-proofs against drift.
        if field in _CLOSEOUT_MUTABLE_TASK_FIELDS:
            continue
        if base_task.get(field) != head_task.get(field):
            reasons.append(
                f"PR must not change frozen governance field {field!r} of its own task {task_id}: it differs "
                "from the base snapshot (ADR 0015 Contract immutability)"
            )
    # risk_level may never resolve WEAKER than the base snapshot, independent of
    # the field-equality rule above: a self-lowered risk would silently weaken the
    # Guardian gate this contract is enforced downstream of. A standalone floor
    # that survives even if the equality rule for risk_level is later relaxed; on a
    # straight downgrade it fires alongside the equality reason (both are true).
    base_risk = str(base_task.get("risk_level") or "")
    head_risk = str(head_task.get("risk_level") or "")
    if _RISK_ORDER.get(head_risk, -1) < _RISK_ORDER.get(base_risk, -1):
        reasons.append(
            f"PR must not lower its own task {task_id} risk_level below the base snapshot "
            f"({base_risk!r} -> {head_risk!r}); a self-lowered risk would weaken the Guardian gate"
        )
    return reasons


def load_contract_approval_registration(path: str | None) -> dict[str, Any] | None:
    """Load the ADR 0015 Contract Approval registration proof from the file named
    by ``--contract-approval``.

    Returns None when the flag is absent, or the file is missing or unreadable, so
    Contract Approval simply does not apply and the Guardian gate behaves exactly
    as it does today. A JSON value that is not an object is likewise treated as
    "no proof" (None), never as a partial proof that might pass by accident.
    """
    if not path:
        return None
    proof_path = Path(path)
    if not proof_path.exists():
        return None
    try:
        data = json.loads(proof_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def contract_approval_for_pr(
    *,
    task_id: str | None,
    head_task: dict[str, Any] | None,
    base_shiki: Path | None,
    changed_files_status: list[ChangedFile],
    registration: dict[str, Any] | None,
) -> Any:
    """Evaluate ADR 0015 Contract Approval for a NORMAL task PR (the OR alternative
    to a live Guardian approval).

    Returns a ``ContractApprovalResult`` from ``shiki_contract_approval``, or None
    when no registration proof was supplied (the ``--contract-approval`` flag
    absent, the file missing/unreadable, or no task id) so the Guardian gate is
    byte-for-byte what it is today. The evaluator is imported LAZILY — never at
    module load — so ``import mergegate_check`` never hard-requires the
    (optionally-staged) ``shiki_contract_approval`` module; an ImportError
    degrades to None ("does not apply"), exactly the ADR 0015 failure mode.
    """
    if registration is None or not task_id:
        return None
    try:
        from shiki_contract_approval import evaluate_contract_approval
    except ImportError:
        return None
    base_task: dict[str, Any] | None = None
    if base_shiki is not None and base_shiki.exists():
        try:
            loaded = load_json(base_shiki / "tasks" / f"{task_id}.json")
        except (OSError, ValueError):
            loaded = None
        base_task = loaded if isinstance(loaded, dict) else None
    return evaluate_contract_approval(
        task_id=task_id,
        base_task=base_task,
        head_task=head_task,
        changed_files_status=changed_files_status,
        registration=registration,
    )


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

        if path.startswith(".shiki/plans/") and path.endswith(".json"):
            state_class = classify_shiki_path(path, manifest or {})
            plan_stem = Path(path).stem
            if entry.status == "D":
                blocking.append(f"PR must not delete Shiki plan {path}; state_class={state_class}")
                continue
            goal = load_goal(target, goal_id)
            source_plan_id = str(goal.get("source_plan") or "") if isinstance(goal, dict) else ""
            if not source_plan_id or plan_stem != source_plan_id:
                blocking.append(
                    f"PR changes plan {path} that is not goal {goal_id}'s source_plan; state_class={state_class}"
                )
                continue
            # Spec Freeze is operator-only (ADR 0009). No PR may author or alter a
            # spec_freeze block — not even on its own goal's source plan, which
            # goal scoping alone would permit: an implementation PR could grant
            # itself a freeze or append amendments to the plan that governs it.
            # Compared against the base snapshot so an unchanged, pre-existing
            # freeze passes; base absence is fail-closed (a freeze present at head
            # with no base to prove it was already there reads as newly authored).
            try:
                head_plan = load_json(target / path)
            except (OSError, ValueError):
                head_plan = None
            head_freeze = head_plan.get("spec_freeze") if isinstance(head_plan, dict) else None
            base_plan = None
            if base_shiki is not None and base_shiki.exists():
                try:
                    base_plan = load_json(base_shiki / "plans" / f"{plan_stem}.json")
                except (OSError, ValueError):
                    base_plan = None
            base_freeze = base_plan.get("spec_freeze") if isinstance(base_plan, dict) else None
            if head_freeze != base_freeze:
                blocking.append(
                    f"PR must not create or modify the spec_freeze block of {path}; "
                    f"Spec Freeze is operator-only; state_class={state_class}"
                )
            continue

        for mirror_dir in _GENERIC_MIRROR_DIRS:
            prefix = f".shiki/{mirror_dir}/"
            if not (path.startswith(prefix) and path.endswith(".json")):
                continue
            state_class = classify_shiki_path(path, manifest or {})
            if entry.status == "D":
                blocking.append(f"PR must not delete Shiki {mirror_dir} record {path}; state_class={state_class}")
            else:
                try:
                    record = load_json(target / path)
                except (OSError, ValueError):
                    record = None
                if not _mirror_record_scoped(record, path, task_id=task_id, goal_id=goal_id):
                    blocking.append(
                        f"PR changes {mirror_dir} record {path} not scoped to task {task_id} or goal {goal_id}; "
                        f"state_class={state_class}"
                    )
            break

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

    # Every tracked mirror directory's base records are delete-protected, not only
    # tasks/goals/locks: a base record absent at HEAD is a deletion the PR must not
    # make. The current task's OWN lock is the sole exception (a release may delete
    # it). ledger is handled by its own byte-equality loop above (it is
    # append-only-evidence, not a mirror class, so it is not in this set).
    for subdir in _tracked_mirror_subdirs(manifest or {}):
        for base_path in protected_base_files(base_shiki, subdir):
            if subdir == "locks" and base_path == lock_file:
                continue
            if not (target / base_path).exists():
                state_class = classify_shiki_path(base_path, manifest or {})
                blocking.append(f"PR must not delete base Shiki {subdir} record {base_path}; state_class={state_class}")


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
    parser.add_argument(
        "--contract-approval",
        default="",
        help=(
            "Path to the ADR 0015 Contract Approval registration proof (JSON). When present and valid, a "
            "normal task PR whose contract was registered and Guardian-approved before dispatch satisfies the "
            "Guardian requirement without a live approval of its own. Absent/missing/unreadable => no effect."
        ),
    )
    parser.add_argument("--guardian-policy", default=".shiki/guardian-policy.json")
    parser.add_argument("--guardian-comments", default=".shiki/gha/live-guardian-comments.json")
    parser.add_argument("--guardian-events", default=".shiki/gha/live-guardian-events.json")
    parser.add_argument("--guardian-timeline", default=".shiki/gha/live-guardian-timeline.json")
    parser.add_argument("--result-file", default=".shiki/gha/mergegate-result.json")
    parser.add_argument("--allow-missing-cca", action="store_true")
    # Base-sync carry (guardian_comment_carried). Inert unless BOTH are given: the
    # flag is off by default and --default-branch defaults to empty, so an
    # invocation that omits them is byte-for-byte the pre-carry gate.
    parser.add_argument(
        "--base-sync-carry",
        action="store_true",
        help="Carry a Guardian comment approval across a proven pure base sync (guardian_comment_carried); requires --default-branch.",
    )
    parser.add_argument(
        "--default-branch",
        default="",
        help="Default branch name; the base the carry proves a pure sync against. Empty disables the carry.",
    )
    args = parser.parse_args()

    target = Path(args.target).resolve()
    blocking: list[str] = []
    warnings: list[str] = []
    resolved_task_id: str | None = None
    resolved_goal_id: str | None = None
    reconcile_mode = False
    post_merge_mode = False
    contract_mode = False
    amendment_mode = False
    migration_mode = False
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
        amendment_mode, amendment_error = amendment_decision(pr)
        migration_mode, migration_error = migration_decision(pr)
        if reconcile_error:
            blocking.append(reconcile_error)
        if post_merge_error:
            blocking.append(post_merge_error)
        if contract_error:
            blocking.append(contract_error)
        if amendment_error:
            blocking.append(amendment_error)
        if migration_error:
            blocking.append(migration_error)
        if reconcile_mode and post_merge_mode:
            blocking.append("a PR cannot be both goal_reconcile and post_merge_reconcile")
        if contract_mode and reconcile_mode:
            blocking.append("a PR cannot be both contract and goal_reconcile")
        if contract_mode and post_merge_mode:
            blocking.append("a PR cannot be both contract and post_merge_reconcile")
        if amendment_mode and reconcile_mode:
            blocking.append("a PR cannot be both amendment and goal_reconcile")
        if amendment_mode and post_merge_mode:
            blocking.append("a PR cannot be both amendment and post_merge_reconcile")
        if amendment_mode and contract_mode:
            blocking.append("a PR cannot be both amendment and contract")
        if migration_mode and reconcile_mode:
            blocking.append("a PR cannot be both migration and goal_reconcile")
        if migration_mode and post_merge_mode:
            blocking.append("a PR cannot be both migration and post_merge_reconcile")
        if migration_mode and contract_mode:
            blocking.append("a PR cannot be both migration and contract")
        if migration_mode and amendment_mode:
            blocking.append("a PR cannot be both migration and amendment")
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
        elif amendment_mode:
            # An Amendment PR is goal-scoped (amends the goal's spec-frozen source
            # plan and re-opens this goal's tasks); it carries no single
            # implementation task id.
            if not resolved_goal_id:
                blocking.append("amendment PR body does not contain a Shiki goal id like G-0001")
        elif post_merge_mode:
            # A post_merge_reconcile PR is task-scoped (reconcile a merged task).
            if not resolved_task_id:
                blocking.append("post_merge_reconcile PR body does not contain a Shiki task id like T-0001")
        elif migration_mode:
            # A Migration PR is repository-wide (a mirror-scoped re-run proof); it
            # references NEITHER a task nor a goal id — it declares a `Migration: <id>`
            # line, validated in enforce_migration.
            pass
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
                    base_sync_carry=args.base_sync_carry,
                    default_branch=args.default_branch,
                )
    elif amendment_mode and pr:
        # Amendment mode (ADR 0009/0015): a goal-scoped PR that corrects a
        # spec-frozen Goal's contract on a protected branch — an append to the
        # source plan's spec_freeze.amendments plus the task files bound to the
        # amended plan. Deny-by-default validator; Guardian gate forced from the
        # goal's frozen plan and INDEPENDENT of a CCA verdict (an amendment carries
        # no implementation for CCA to judge). Like contract mode, the Guardian
        # requirement runs here rather than in the single-task CCA flow.
        files_status = parse_changed_files_status(
            Path(args.changed_files_status), changed_files(Path(args.changed_files))
        )
        base_shiki_amend = Path(args.base_shiki) if args.base_shiki else None
        enforce_amendment(
            target=target,
            goal_id=resolved_goal_id or "",
            base_shiki=base_shiki_amend,
            changed_files_status=files_status,
            blocking=blocking,
            warnings=warnings,
        )
        if resolved_goal_id:
            # Reuse contract_guardian_risk on BOTH the head and the base snapshot
            # and take the stronger, so an amendment that WEAKENS its own plan's
            # risk_level can never lower its own Guardian gate below what the
            # pre-amendment (base) contract required. No second resolution — the
            # existing resolver runs against each root and _max_risk combines them.
            head_risk = contract_guardian_risk(target, resolved_goal_id) or ""
            base_risk = ""
            if base_shiki_amend is not None and base_shiki_amend.exists():
                base_risk = contract_guardian_risk(base_shiki_amend.parent, resolved_goal_id) or ""
            amendment_risk = _max_risk(head_risk, base_risk) or None
            if amendment_risk not in _RISK_ORDER:
                # Fail closed: an unresolvable amendment risk means the Guardian gate
                # cannot be evaluated, so the amendment must not slip through as if
                # it were unguarded.
                blocking.append(
                    f"amendment PR Guardian risk for goal {resolved_goal_id} could not be resolved "
                    f"from its frozen plan (got {amendment_risk!r}); the Guardian gate cannot be evaluated"
                )
            else:
                enforce_guardian_policy(
                    pr=pr,
                    task={"risk_level": amendment_risk},
                    target=target,
                    guardian_policy=args.guardian_policy,
                    guardian_comments=args.guardian_comments,
                    guardian_events=args.guardian_events,
                    guardian_timeline=args.guardian_timeline,
                    blocking=blocking,
                    warnings=warnings,
                    expected_repository=args.expected_repository,
                )
    elif migration_mode and pr:
        # Migration mode: a repository-wide mirror change whose sole proof
        # obligation is that the .shiki diff EQUALS the re-run output of a migration
        # registered on the BASE branch (see enforce_migration). It carries no
        # implementation task, CCA verdict, or Guardian gate — the maintainer label
        # is the authorization and the base-registered re-run is the proof.
        files_status = parse_changed_files_status(
            Path(args.changed_files_status), changed_files(Path(args.changed_files))
        )
        enforce_migration(
            target=target,
            pr=pr,
            base_shiki=Path(args.base_shiki) if args.base_shiki else None,
            changed_files_status=files_status,
            blocking=blocking,
            warnings=warnings,
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
            # Judge the lock-scope gates against the WHOLE diff, both legs of every
            # rename included. Using the name-only `files` list would let a rename
            # move a file out of locked scope unseen (git shows only the new leg).
            scope_paths = lock_scope_paths(files_status)
            for path in files_outside_locks(scope_paths, effective_locks):
                blocking.append(f"Changed file {path} is outside declared task locks")
            blocking.extend(active_lock_conflicts(target, resolved_task_id, locks, scope_paths))

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
                # ADR 0015 Contract immutability on the NORMAL task-PR path: a PR
                # that changes its own task file may move only bookkeeping — its
                # frozen governance contract is bound to the base snapshot, and its
                # risk_level may never resolve weaker than base. Only this branch
                # calls it (contract / goal_reconcile / post_merge_reconcile use
                # their own validators), so nothing is double-reported.
                blocking.extend(
                    normal_task_contract_immutability_reasons(
                        base_shiki=Path(args.base_shiki) if args.base_shiki else None,
                        task_id=resolved_task_id,
                        head_task=task,
                        changed_files_status=files_status,
                    )
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
            blocking.append("CCA verdict did not set can_merge=true")
        failures = blocking_checklist_failures(cca)
        if failures:
            blocking.append("CCA verdict contains blocking failed checklist items: " + ", ".join(failures))
        if cca.get("verdict") == "complete":
            acceptance_failures = failing_acceptance_criteria(cca)
            if acceptance_failures:
                blocking.append(
                    "complete CCA verdict contains failing acceptance criteria: " + ", ".join(acceptance_failures)
                )
        for reason in checklist_profile_coverage_failures(task, cca):
            blocking.append(reason)
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
            elif task is not None and args.base_shiki and resolved_task_id:
                # Normal path (ADR 0015): resolve the Guardian risk NEVER WEAKER
                # than the base snapshot, so a PR that lowered its own task
                # risk_level cannot dissolve its Guardian requirement — the gate
                # still fires at the base risk. This is the independent floor the
                # acceptance requires: the guardian outcome survives even if the
                # immutability block above were bypassed. A no-op when the task
                # file is unchanged (base risk == head risk) or on an upgrade.
                base_shiki_dir = Path(args.base_shiki)
                base_task_for_risk: dict[str, Any] | None = None
                if base_shiki_dir.exists():
                    try:
                        base_task_for_risk = load_json(base_shiki_dir / "tasks" / f"{resolved_task_id}.json")
                    except (OSError, ValueError):
                        # An unreadable base leaves the head risk in force here; the
                        # immutability gate above has already blocked the change.
                        base_task_for_risk = None
                resolved_risk = resolved_guardian_risk_never_weaker(base_task_for_risk, task)
                if resolved_risk and resolved_risk != str(task.get("risk_level") or ""):
                    guardian_task = {**task, "risk_level": resolved_risk}
            # ADR 0015 Contract Approval applies ONLY on the normal task path: a
            # Contract PR (contract mode) is the registration itself and must be
            # approved live, and reconcile PRs carry no implementation task. When
            # the flag is absent, this is None and the Guardian gate is unchanged.
            contract_approval = None
            if task is not None and not reconcile_mode and not post_merge_mode and not contract_mode:
                contract_approval = contract_approval_for_pr(
                    task_id=resolved_task_id,
                    head_task=task,
                    base_shiki=Path(args.base_shiki) if args.base_shiki else None,
                    changed_files_status=files_status,
                    registration=load_contract_approval_registration(args.contract_approval),
                )
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
                contract_approval=contract_approval,
                base_sync_carry=args.base_sync_carry,
                default_branch=args.default_branch,
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
