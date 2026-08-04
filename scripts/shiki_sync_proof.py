#!/usr/bin/env python3
"""Prove from git alone that a PR push was a *pure base sync* — no content change.

Branch protection with ``required_status_checks.strict = true`` forces a PR that
fell BEHIND to merge its base in before it can merge. That advances the head SHA,
and ``guardian_comment.require_head_sha`` then discards the approval already
granted for the reviewed content: one fresh operator approval per PR per merge
round, for zero content change. Measured on 2026-08-01, landing five Contract PRs
cost four extra approvals that way.

:func:`verify_pure_base_sync` decides, from git, whether the move from ``a_sha``
(the reviewed head) to ``head_sha`` (the current head) is a pure base sync: a
first-parent chain of ordinary two-parent merges of the branch's own base that
carries no content a mechanical merge would not itself produce. When it is, the
earlier approval can be carried without a re-review.

The criterion is TREE REPRODUCTION (step 9), not a diff. Two cheaper checks both
fail here, as measured in throwaway repositories:

* ``git merge -s ours <base>`` yields an identical ``git diff A H`` while
  reverting the base entirely; and
* an "evil merge" can carry an attacker-chosen payload while touching only paths
  the base itself touched.

Only re-deriving the mechanical merge tree of ``a_sha`` and the base and
demanding it equal ``tree(head_sha)`` rejects them. Steps 6-8 are pre-rejects
that bound which ``a_sha`` may be named; step 9 is the proof, and there is no
branch that returns ``proved=True`` without step 9 having run and matched.

This is the ONLY module in the guardian family that runs git. Every git
invocation here is read-only, runs with ``cwd=target``, and has its return code
inspected — never swallowed, never chained to a shell that would discard the
exit status. An older runner (``git merge-tree --write-tree`` needs git >= 2.38,
which this repository does not otherwise verify) therefore refuses the carry
loudly at the capability probe instead of silently mis-proving one. Every git
call is local: no invocation is a network or API lookup, because
an API lookup would serve commits orphaned by a force-push, which is exactly the
case step 4 exists to reject.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess

_SHA40 = re.compile(r"[0-9a-f]{40}")

# git subcommands this module is permitted to run. Every one is read-only; the
# family invariant is that shiki_sync_proof never mutates a repository and never
# reaches the network, so there is deliberately no ``fetch``/``ls-remote``/
# ``clone`` here and no ``gh`` anywhere in the module.
_READ_ONLY_GIT = frozenset(
    {"rev-parse", "merge-tree", "cat-file", "ls-tree", "rev-list", "merge-base"}
)


@dataclass(frozen=True)
class SyncProof:
    """The verdict of :func:`verify_pure_base_sync`.

    ``proved`` is True only when tree reproduction (step 9) ran and matched.
    ``reason`` names the first failing condition (prefixed ``step N:``) or the
    exception, and is empty on a proof. ``merge_count`` is the number of
    first-parent sync merges carried between ``a_sha`` and ``head_sha``.
    """

    proved: bool
    reason: str
    merge_count: int = 0


class _GitError(RuntimeError):
    """A git invocation failed in a way the proof does not model as a verdict.

    Raised only for unexpected failures of a git call the proof expects to
    succeed. It propagates to the function boundary, where it is caught and
    named — it is never silently swallowed to continue the proof.
    """


def _git(
    target: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a read-only git command in ``target`` and return the completed process.

    With ``check=True`` a non-zero exit raises :class:`_GitError` (the caller
    expected success). With ``check=False`` the caller inspects ``returncode``
    itself and turns an expected non-zero exit into a distinct verdict — the
    failure is examined, never swallowed.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=str(target),
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise _GitError(f"git {' '.join(args)} failed (exit {proc.returncode}): {detail}")
    return proc


def _tree_has_gitattributes(target: Path, commit: str) -> bool:
    """True when any tree entry of ``commit`` is named ``.gitattributes``.

    A ``.gitattributes`` at any depth can set ``merge=union`` (or another custom
    driver) and reprogram what a merge produces, so its mere presence forfeits
    the proof.
    """
    out = _git(target, "ls-tree", "-r", "--name-only", commit).stdout
    return any(Path(line).name == ".gitattributes" for line in out.splitlines() if line)


def verify_pure_base_sync(
    *, target: object, a_sha: str, head_sha: str, default_branch: str
) -> SyncProof:
    """Prove, from git in ``target``, that ``a_sha`` -> ``head_sha`` is a pure base sync.

    Returns a :class:`SyncProof`. Any exception is caught at this boundary and
    returned as ``proved=False`` with the exception named; the function never
    returns ``proved=True`` unless step 9 (tree reproduction) ran and matched.
    """
    try:
        return _prove(Path(str(target)), str(a_sha), str(head_sha), str(default_branch))
    except _GitError as exc:
        return SyncProof(False, f"git invocation failed: {exc}", 0)
    except Exception as exc:  # boundary: name the failure and stop, never continue
        return SyncProof(False, f"unexpected error: {exc!r}", 0)


def _prove(target: Path, a_sha: str, head_sha: str, default_branch: str) -> SyncProof:
    # --- Step 1: a real, non-shallow repo; the base resolves; HEAD is head_sha. ---
    if _git(target, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        return SyncProof(
            False,
            "step 1: repository is shallow; refusing to prove a base sync from truncated history",
            0,
        )
    base_ref = f"refs/remotes/origin/{default_branch}^{{commit}}"
    base_proc = _git(target, "rev-parse", "--verify", "--quiet", base_ref, check=False)
    if base_proc.returncode != 0:
        return SyncProof(
            False,
            f"step 1: base ref refs/remotes/origin/{default_branch} did not resolve to a commit",
            0,
        )
    base_sha = base_proc.stdout.strip()
    actual_head = _git(target, "rev-parse", "HEAD").stdout.strip()
    if actual_head != head_sha:
        return SyncProof(
            False,
            f"step 1: working tree HEAD {actual_head} does not equal head_sha {head_sha}",
            0,
        )

    # --- Step 2: merge-tree --write-tree capability probe (needs git >= 2.38). ---
    # Merging head with itself is always a clean, no-op merge, so on a capable git
    # it prints a 40-hex tree and exits 0; an older git fails the unknown option
    # and we refuse the carry loudly here rather than mis-proving it downstream.
    probe = _git(
        target,
        "-c",
        "core.attributesFile=/dev/null",
        "merge-tree",
        "--write-tree",
        actual_head,
        actual_head,
        check=False,
    )
    probe_lines = probe.stdout.splitlines()
    probe_line = probe_lines[0].strip() if probe_lines else ""
    if probe.returncode != 0 or not _SHA40.fullmatch(probe_line):
        return SyncProof(
            False,
            "step 2: `git merge-tree --write-tree` capability probe failed; git >= 2.38 required to prove a base sync",
            0,
        )

    # --- Step 3: no in-tree .gitattributes can reprogram the merge (merge=union). ---
    # The a_sha tree is inspected below, once step 4 has proven a_sha is a local
    # object — its tree cannot be listed before then.
    for label, commit in (("head_sha", actual_head), ("base", base_sha)):
        if _tree_has_gitattributes(target, commit):
            return SyncProof(
                False,
                f"step 3: a .gitattributes path exists in the {label} tree; a merge attribute could reprogram the merge",
                0,
            )

    # --- Step 4: a_sha must be a LOCAL object — never a network or API lookup. ---
    # An API lookup would resolve a commit orphaned by a force-push; that is the
    # attack this condition rejects, so resolution stays strictly local.
    if _git(target, "cat-file", "-e", a_sha, check=False).returncode != 0:
        return SyncProof(
            False,
            f"step 4: a_sha {a_sha} is not a local git object; refusing a network or API lookup",
            0,
        )
    a_oid = _git(target, "rev-parse", "--verify", f"{a_sha}^{{commit}}").stdout.strip()
    if _tree_has_gitattributes(target, a_oid):
        return SyncProof(
            False,
            "step 3: a .gitattributes path exists in the a_sha tree; a merge attribute could reprogram the merge",
            0,
        )

    # --- Step 5: an unchanged head needs no carry. ---
    if a_oid == actual_head:
        return SyncProof(False, "step 5: a_sha equals head_sha; there is no base sync to prove", 0)

    # --- Step 6: a_sha must sit on the first-parent history of head. ---
    first_parents = _git(target, "rev-list", "--first-parent", actual_head).stdout.split()
    if a_oid not in first_parents:
        return SyncProof(
            False,
            f"step 6: a_sha {a_oid} is not on the first-parent history of head_sha; the merge direction is reversed or a_sha is unrelated",
            0,
        )

    # --- Step 7: no authored (non-merge) commit may sit on that path. ---
    authored = _git(
        target, "rev-list", "--first-parent", "--no-merges", f"{a_oid}..{actual_head}"
    ).stdout.split()
    if authored:
        return SyncProof(
            False,
            f"step 7: {len(authored)} authored non-merge commit(s) on the first-parent path {a_oid[:12]}..{actual_head[:12]}; a pure base sync adds only merges",
            0,
        )

    # --- Step 8: every carried merge is an ordinary two-parent merge of the base. ---
    merges = _git(target, "rev-list", "--first-parent", f"{a_oid}..{actual_head}").stdout.split()
    merge_count = len(merges)
    for commit in merges:
        parents = _git(target, "rev-list", "--parents", "-n", "1", commit).stdout.split()[1:]
        if len(parents) != 2:
            return SyncProof(
                False,
                f"step 8: first-parent commit {commit} has {len(parents)} parents, not exactly two; not an ordinary base-sync merge",
                merge_count,
            )
        second_parent = parents[1]
        if (
            _git(target, "merge-base", "--is-ancestor", second_parent, base_sha, check=False).returncode
            != 0
        ):
            return SyncProof(
                False,
                f"step 8: second parent {second_parent} of merge {commit} is not an ancestor of base {base_sha}",
                merge_count,
            )

    # --- Step 9 (THE PROOF): the mechanical merge of a_sha and the base must ---
    # --- re-derive exactly tree(head_sha). Nothing returns proved=True before here. ---
    merged = _git(
        target,
        "-c",
        "core.attributesFile=/dev/null",
        "merge-tree",
        "--write-tree",
        a_oid,
        base_sha,
        check=False,
    )
    if merged.returncode != 0:
        return SyncProof(
            False,
            f"step 9: merging a_sha into base conflicts (git merge-tree exit {merged.returncode}); the carry needs a real resolution, not an automatic sync",
            merge_count,
        )
    merged_lines = merged.stdout.splitlines()
    merged_tree = merged_lines[0].strip() if merged_lines else ""
    head_tree = _git(target, "rev-parse", f"{actual_head}^{{tree}}").stdout.strip()
    if merged_tree != head_tree:
        return SyncProof(
            False,
            f"step 9: the mechanical merge tree {merged_tree} does not equal head tree {head_tree}; head carries content a pure base sync would not produce",
            merge_count,
        )
    return SyncProof(True, "", merge_count)
