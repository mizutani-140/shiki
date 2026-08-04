"""verify_pure_base_sync proves a push was a pure base sync — from real git.

Every test here builds a REAL temporary git repository; git is never mocked. The
one exception, the capability-probe test, wraps the real git binary in a thin
PATH shim that fails only ``merge-tree`` (to simulate a pre-2.38 runner) — the
repository and every other git call are still real.

The proof's criterion is TREE REPRODUCTION (step 9). The tree-reproduction tests
therefore assert *explicitly*, via independent raw-git queries, that the
first-parent / no-authored-commit / arity pre-rejects (steps 6-8) all PASS, so
that a refusal there is proven to come from step 9 and step 9 alone — it is the
load-bearing check, not redundant with the cheaper ones.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_installer
import shiki_sync_proof
from shiki_sync_proof import verify_pure_base_sync


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check, capture_output=True, text=True
    )


def _rev(cwd: Path, ref: str) -> str:
    return _git(cwd, "rev-parse", ref).stdout.strip()


class _Repo:
    """A tiny builder over a real git repository in ``path``."""

    def __init__(self, path: Path):
        self.path = path
        _git(path, "init", "-q", "-b", "main")
        _git(path, "config", "user.email", "t@t")
        _git(path, "config", "user.name", "t")

    def commit(self, files: dict[str, str], message: str) -> str:
        for name, content in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-qm", message)
        return self.head()

    def checkout(self, ref: str, *, new_branch: str | None = None) -> None:
        if new_branch:
            _git(self.path, "checkout", "-q", "-b", new_branch, ref)
        else:
            _git(self.path, "checkout", "-q", ref)

    def merge(self, other: str, *, message: str = "sync", ours: bool = False) -> str:
        args = ["merge", "-q", "--no-ff"]
        if ours:
            args += ["-s", "ours"]
        args += ["-m", message, other]
        _git(self.path, *args)
        return self.head()

    def commit_tree(self, tree: str, parents: list[str], message: str = "forge") -> str:
        args = ["commit-tree", tree]
        for parent in parents:
            args += ["-p", parent]
        args += ["-m", message]
        return _git(self.path, *args).stdout.strip()

    def tree_of(self, ref: str) -> str:
        return _rev(self.path, f"{ref}^{{tree}}")

    def set_origin_main(self, sha: str) -> None:
        _git(self.path, "update-ref", "refs/remotes/origin/main", sha)

    def set_head(self, sha: str) -> None:
        # Force a detached checkout so commit-tree-forged heads (and dirty
        # indexes left by an evil-merge fixture) land cleanly.
        _git(self.path, "checkout", "-q", "-f", sha)

    def head(self) -> str:
        return _rev(self.path, "HEAD")


class _GitSpy:
    """Records every ``subprocess.run`` the module makes, then calls the real one.

    This is a passthrough spy, not a mock: real git still runs against the real
    repository. It exists only to observe the argv/cwd of each invocation.
    """

    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []

    def __enter__(self) -> "_GitSpy":
        real = subprocess.run

        def spy(cmd, *a, **k):
            self.calls.append((list(cmd), dict(k)))
            return real(cmd, *a, **k)

        self._patch = mock.patch.object(shiki_sync_proof.subprocess, "run", side_effect=spy)
        self._patch.start()
        return self

    def __exit__(self, *exc) -> bool:
        self._patch.stop()
        return False

    def subcommands(self) -> list[str | None]:
        return [_subcommand(cmd) for cmd, _ in self.calls]

    def merge_tree_pairs(self) -> list[tuple[str, str]]:
        pairs = []
        for cmd, _ in self.calls:
            if "merge-tree" in cmd:
                pairs.append((cmd[-2], cmd[-1]))
        return pairs


def _subcommand(cmd: list[str]) -> str | None:
    """The git subcommand, skipping a leading ``-c key=value`` pair."""
    i = 1
    while i < len(cmd):
        if cmd[i] == "-c":
            i += 2
            continue
        return cmd[i]
    return None


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.repo = _Repo(self.dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def verify(self, a: str, head: str, *, branch: str = "main"):
        return verify_pure_base_sync(
            target=self.dir, a_sha=a, head_sha=head, default_branch=branch
        )

    def base_oid(self) -> str:
        return _rev(self.dir, "refs/remotes/origin/main")

    def assert_pre_steps_pass(self, a: str, head: str, base: str) -> None:
        """Independently prove, via raw git, that steps 6-8 PASS for this shape."""
        first_parents = _git(self.dir, "rev-list", "--first-parent", head).stdout.split()
        self.assertIn(a, first_parents, "step 6 (a_sha on first-parent history) should pass")
        authored = _git(
            self.dir, "rev-list", "--first-parent", "--no-merges", f"{a}..{head}"
        ).stdout.split()
        self.assertEqual(authored, [], "step 7 (no authored non-merge commit) should pass")
        merges = _git(self.dir, "rev-list", "--first-parent", f"{a}..{head}").stdout.split()
        self.assertTrue(merges, "there must be at least one carried merge")
        for commit in merges:
            parents = _git(self.dir, "rev-list", "--parents", "-n", "1", commit).stdout.split()[1:]
            self.assertEqual(len(parents), 2, f"step 8 arity for {commit} should pass")
            rc = _git(
                self.dir, "merge-base", "--is-ancestor", parents[1], base, check=False
            ).returncode
            self.assertEqual(rc, 0, f"step 8 ancestry for {commit} should pass")

    # -- shared fixtures -------------------------------------------------------

    def build_single_clean_sync(self) -> tuple[str, str]:
        """One base sync of disjoint work; origin/main sits at the merged base."""
        b0 = self.repo.commit({"shared.txt": "base\n", "README.md": "r\n"}, "base")
        self.repo.checkout(b0, new_branch="feat")
        a = self.repo.commit({"feature.txt": "feature\n"}, "A")
        self.repo.checkout("main")
        b1 = self.repo.commit({"other.txt": "o1\n"}, "B1")
        self.repo.checkout("feat")
        m = self.repo.merge(b1)
        self.repo.set_origin_main(b1)
        return a, m


class PositiveProofTests(_Base):
    def test_single_clean_sync_proves(self) -> None:
        a, m = self.build_single_clean_sync()
        proof = self.verify(a, m)
        self.assertTrue(proof.proved, proof.reason)
        self.assertEqual(proof.reason, "")
        self.assertEqual(proof.merge_count, 1)

    def test_two_consecutive_clean_syncs_prove(self) -> None:
        b0 = self.repo.commit({"shared.txt": "base\n", "README.md": "r\n"}, "base")
        self.repo.checkout(b0, new_branch="feat")
        a = self.repo.commit({"feature.txt": "feature\n"}, "A")
        self.repo.checkout("main")
        b1 = self.repo.commit({"other.txt": "o1\n"}, "B1")
        b2 = self.repo.commit({"other2.txt": "o2\n"}, "B2")
        self.repo.checkout("feat")
        self.repo.merge(b1, message="s1")
        m2 = self.repo.merge(b2, message="s2")
        self.repo.set_origin_main(b2)
        proof = self.verify(a, m2)
        self.assertTrue(proof.proved, proof.reason)
        self.assertEqual(proof.merge_count, 2)

    def test_base_that_did_not_move_proves(self) -> None:
        # The base never advanced past the fork point, yet the loop still records
        # a merge of it. Its tree equals tree(A), so the mechanical merge of A and
        # the (unmoved) base re-derives it exactly.
        b0 = self.repo.commit({"shared.txt": "base\n"}, "base")
        self.repo.checkout(b0, new_branch="feat")
        a = self.repo.commit({"feature.txt": "feature\n"}, "A")
        merge = self.repo.commit_tree(self.repo.tree_of(a), [a, b0], message="sync-noop")
        self.repo.set_head(merge)
        self.repo.set_origin_main(b0)
        proof = self.verify(a, merge)
        self.assertTrue(proof.proved, proof.reason)
        self.assertEqual(proof.merge_count, 1)


class TreeReproductionIsLoadBearingTests(_Base):
    """Step 9 rejects moves that every cheaper check (steps 6-8) accepts."""

    def test_evil_merge_payload_in_base_touched_path_refused_only_by_step9(self) -> None:
        b0 = self.repo.commit({"shared.txt": "base\n", "README.md": "r\n"}, "base")
        self.repo.checkout(b0, new_branch="feat")
        a = self.repo.commit({"feature.txt": "feature\n"}, "A")
        self.repo.checkout("main")
        b1 = self.repo.commit({"shared.txt": "base-v2\n"}, "B1")  # base touches shared.txt
        self.repo.checkout("feat")
        self.repo.merge(b1)  # real merge; working tree now holds the true merge
        (self.dir / "shared.txt").write_text("ATTACKER\n")  # payload in a base-touched path
        _git(self.dir, "add", "shared.txt")
        evil_tree = _git(self.dir, "write-tree").stdout.strip()
        evil = self.repo.commit_tree(evil_tree, [a, b1], message="evil")
        self.repo.set_head(evil)
        self.repo.set_origin_main(b1)

        # The payload lands only in shared.txt — a path the base itself touched —
        # so a "changed paths ⊆ base-touched paths" check would wave it through.
        changed = _git(self.dir, "diff", "--name-only", a, evil).stdout.split()
        self.assertEqual(changed, ["shared.txt"])

        self.assert_pre_steps_pass(a, evil, self.base_oid())
        proof = self.verify(a, evil)
        self.assertFalse(proof.proved)
        self.assertTrue(proof.reason.startswith("step 9"), proof.reason)
        self.assertIn("does not equal head tree", proof.reason)

    def test_merge_s_ours_reverts_base_and_is_refused_by_step9(self) -> None:
        b0 = self.repo.commit({"shared.txt": "base\n"}, "base")
        self.repo.checkout(b0, new_branch="feat")
        a = self.repo.commit({"feature.txt": "feature\n"}, "A")
        self.repo.checkout("main")
        b1 = self.repo.commit({"shared.txt": "base-v2\n"}, "B1")
        self.repo.checkout("feat")
        m = self.repo.merge(b1, ours=True)
        self.repo.set_origin_main(b1)

        # `-s ours` keeps A's tree wholesale, so A..M is an empty diff even though
        # the base's shared.txt change was silently reverted.
        self.assertEqual(_git(self.dir, "diff", "--name-only", a, m).stdout.strip(), "")

        self.assert_pre_steps_pass(a, m, self.base_oid())
        proof = self.verify(a, m)
        self.assertFalse(proof.proved)
        self.assertTrue(proof.reason.startswith("step 9"), proof.reason)

    def test_superseded_stale_base_passes_arity_refused_by_step9(self) -> None:
        # The merge carried a stale base tip (B1). It IS reachable from the current
        # base (B2), so step 8 passes — but merging the current base does not
        # reproduce head, so step 9 refuses.
        b0 = self.repo.commit({"shared.txt": "base\n"}, "base")
        self.repo.checkout(b0, new_branch="feat")
        a = self.repo.commit({"feature.txt": "feature\n"}, "A")
        self.repo.checkout("main")
        b1 = self.repo.commit({"other.txt": "o1\n"}, "B1")
        b2 = self.repo.commit({"other2.txt": "o2\n"}, "B2")
        self.repo.checkout("feat")
        m1 = self.repo.merge(b1)
        self.repo.set_origin_main(b2)  # base has advanced past the merged B1

        self.assertEqual(
            _git(self.dir, "merge-base", "--is-ancestor", b1, b2, check=False).returncode,
            0,
            "the stale tip must be reachable from the current base",
        )
        self.assert_pre_steps_pass(a, m1, self.base_oid())
        proof = self.verify(a, m1)
        self.assertFalse(proof.proved)
        self.assertTrue(proof.reason.startswith("step 9"), proof.reason)

    def test_commit_tree_forged_merge_with_correct_parents_refused_by_step9(self) -> None:
        b0 = self.repo.commit({"shared.txt": "base\n"}, "base")
        self.repo.checkout(b0, new_branch="feat")
        a = self.repo.commit({"feature.txt": "feature\n"}, "A")
        self.repo.checkout("main")
        b1 = self.repo.commit({"other.txt": "o1\n"}, "B1")
        # Correct parents [A, B1], but an arbitrary tree (the base's own tree,
        # which lacks feature.txt).
        forged = self.repo.commit_tree(self.repo.tree_of(b0), [a, b1], message="forged")
        self.repo.set_head(forged)
        self.repo.set_origin_main(b1)

        self.assert_pre_steps_pass(a, forged, self.base_oid())
        proof = self.verify(a, forged)
        self.assertFalse(proof.proved)
        self.assertTrue(proof.reason.startswith("step 9"), proof.reason)


class PreRejectTests(_Base):
    """Each cheaper condition refuses its own shape, before step 9."""

    def test_gitattributes_union_refused_by_step3_before_step9(self) -> None:
        b0 = self.repo.commit({"shared.txt": "base\n"}, "base")
        self.repo.checkout(b0, new_branch="feat")
        a = self.repo.commit({"feature.txt": "feature\n"}, "A")
        self.repo.checkout("main")
        b1 = self.repo.commit(
            {".gitattributes": "shared.txt merge=union\n", "other.txt": "o1\n"}, "B1"
        )
        self.repo.checkout("feat")
        m = self.repo.merge(b1)
        self.repo.set_origin_main(b1)

        with _GitSpy() as spy:
            proof = self.verify(a, m)

        self.assertFalse(proof.proved)
        self.assertTrue(proof.reason.startswith("step 3"), proof.reason)
        self.assertIn(".gitattributes", proof.reason)
        # Step 9 (merge-tree of a_sha and the base) must never have run.
        pairs = spy.merge_tree_pairs()
        self.assertNotIn((a, self.base_oid()), pairs, "step 9 must not run after step 3 refuses")

    def test_octopus_merge_refused_by_arity(self) -> None:
        b0 = self.repo.commit({"shared.txt": "base\n"}, "base")
        self.repo.checkout(b0, new_branch="feat")
        a = self.repo.commit({"feature.txt": "feature\n"}, "A")
        self.repo.checkout(b0, new_branch="side")
        c = self.repo.commit({"side.txt": "s\n"}, "C")
        self.repo.checkout("main")
        b1 = self.repo.commit({"other.txt": "o1\n"}, "B1")
        octopus = self.repo.commit_tree(self.repo.tree_of(a), [a, b1, c], message="octo")
        self.repo.set_head(octopus)
        self.repo.set_origin_main(b1)

        proof = self.verify(a, octopus)
        self.assertFalse(proof.proved)
        self.assertTrue(proof.reason.startswith("step 8"), proof.reason)
        self.assertIn("not exactly two", proof.reason)

    def test_authored_non_merge_commit_refused_by_step7(self) -> None:
        b0 = self.repo.commit({"shared.txt": "base\n"}, "base")
        self.repo.checkout(b0, new_branch="feat")
        a = self.repo.commit({"feature.txt": "feature\n"}, "A")
        self.repo.checkout("main")
        b1 = self.repo.commit({"other.txt": "o1\n"}, "B1")
        self.repo.checkout(a)
        authored = self.repo.commit({"extra.txt": "authored\n"}, "authored")
        self.repo.set_origin_main(b1)

        proof = self.verify(a, authored)
        self.assertFalse(proof.proved)
        self.assertTrue(proof.reason.startswith("step 7"), proof.reason)
        self.assertIn("authored non-merge", proof.reason)

    def test_reversed_merge_direction_refused_by_step6(self) -> None:
        b0 = self.repo.commit({"shared.txt": "base\n"}, "base")
        self.repo.checkout(b0, new_branch="feat")
        a = self.repo.commit({"feature.txt": "feature\n"}, "A")
        self.repo.checkout("main")
        b1 = self.repo.commit({"other.txt": "o1\n"}, "B1")
        # The base merges the feature: parents are [B1, A]; A is the SECOND parent.
        m = self.repo.merge(a, message="base merges feature")
        self.repo.set_origin_main(b1)

        proof = self.verify(a, m)
        self.assertFalse(proof.proved)
        self.assertTrue(proof.reason.startswith("step 6"), proof.reason)
        self.assertIn("first-parent history", proof.reason)

    def test_second_parent_not_ancestor_of_base_refused_by_step8(self) -> None:
        b0 = self.repo.commit({"shared.txt": "base\n"}, "base")
        self.repo.checkout(b0, new_branch="feat")
        a = self.repo.commit({"feature.txt": "feature\n"}, "A")
        self.repo.checkout(b0, new_branch="rogue")
        x = self.repo.commit({"rogue.txt": "x\n"}, "X")  # not on origin/main
        self.repo.checkout("main")
        b1 = self.repo.commit({"other.txt": "o1\n"}, "B1")
        self.repo.checkout("feat")
        m = self.repo.merge(x)  # merge an off-base commit
        self.repo.set_origin_main(b1)

        proof = self.verify(a, m)
        self.assertFalse(proof.proved)
        self.assertTrue(proof.reason.startswith("step 8"), proof.reason)
        self.assertIn("not an ancestor of base", proof.reason)

    def test_unchanged_head_refused_by_step5(self) -> None:
        a, _ = self.build_single_clean_sync()
        # HEAD must equal head_sha for step 1; check a==head at that exact head.
        self.repo.set_head(a)
        self.repo.set_origin_main(a)
        proof = self.verify(a, a)
        self.assertFalse(proof.proved)
        self.assertTrue(proof.reason.startswith("step 5"), proof.reason)
        self.assertIn("no base sync to prove", proof.reason)


class LocalityTests(_Base):
    def test_orphaned_a_sha_refused_by_step4_without_a_network_call(self) -> None:
        a, m = self.build_single_clean_sync()
        # An a_sha that is a REAL commit somewhere else but not a local object —
        # exactly what a force-push orphans and an API lookup would resurrect.
        with tempfile.TemporaryDirectory() as other:
            other_repo = _Repo(Path(other))
            orphan = other_repo.commit({"gone.txt": "orphan\n"}, "orphan")
        self.assertEqual(
            _git(self.dir, "cat-file", "-e", orphan, check=False).returncode,
            1,
            "the orphan must not be a local object",
        )

        with _GitSpy() as spy:
            proof = self.verify(orphan, m)

        self.assertFalse(proof.proved)
        self.assertTrue(proof.reason.startswith("step 4"), proof.reason)
        self.assertIn("not a local git object", proof.reason)

        # No network or API lookup was attempted to resolve the orphan.
        for cmd, _ in spy.calls:
            self.assertEqual(cmd[0], "git", f"only git may be spawned, saw {cmd[0]}")
        network = {"fetch", "ls-remote", "pull", "clone", "remote", "fetch-pack"}
        self.assertFalse(
            network & set(spy.subcommands()),
            f"a network git subcommand was attempted: {spy.subcommands()}",
        )

    def test_shallow_repository_refused_with_distinct_reason(self) -> None:
        # A real shallow clone (depth 1 over file://).
        source = self.repo
        source.commit({"a.txt": "1\n"}, "c1")
        source.commit({"b.txt": "2\n"}, "c2")
        with tempfile.TemporaryDirectory() as shallow_parent:
            shallow_dir = Path(shallow_parent) / "shallow"
            subprocess.run(
                ["git", "clone", "-q", "--depth", "1", f"file://{self.dir}", str(shallow_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                _git(shallow_dir, "rev-parse", "--is-shallow-repository").stdout.strip(),
                "true",
            )
            head = _rev(shallow_dir, "HEAD")
            proof = verify_pure_base_sync(
                target=shallow_dir, a_sha=head, head_sha=head, default_branch="main"
            )
        self.assertFalse(proof.proved)
        self.assertTrue(proof.reason.startswith("step 1"), proof.reason)
        self.assertIn("shallow", proof.reason)

    def test_missing_merge_tree_capability_refused_by_step2(self) -> None:
        a, m = self.build_single_clean_sync()
        real_git = _require_real_git()
        with tempfile.TemporaryDirectory() as shim_dir:
            shim = Path(shim_dir) / "git"
            # A pre-2.38 git rejects `merge-tree --write-tree`; simulate that by
            # failing only merge-tree and delegating everything else to real git.
            shim.write_text(
                "#!/bin/sh\n"
                'for a in "$@"; do\n'
                '  if [ "$a" = "merge-tree" ]; then\n'
                "    echo \"error: unknown option 'write-tree'\" >&2\n"
                "    exit 129\n"
                "  fi\n"
                "done\n"
                f'exec "{real_git}" "$@"\n'
            )
            shim.chmod(0o755)
            patched_path = shim_dir + os.pathsep + os.environ.get("PATH", "")
            with mock.patch.dict(os.environ, {"PATH": patched_path}):
                proof = self.verify(a, m)

        self.assertFalse(proof.proved)
        self.assertTrue(proof.reason.startswith("step 2"), proof.reason)
        self.assertIn("capability probe", proof.reason)


class GitDisciplineTests(_Base):
    """The family invariant: read-only, target-scoped, never networked, no swallow."""

    def test_every_git_call_is_read_only_and_scoped_to_target(self) -> None:
        a, m = self.build_single_clean_sync()
        with _GitSpy() as spy:
            proof = self.verify(a, m)
        self.assertTrue(proof.proved, proof.reason)
        self.assertTrue(spy.calls, "the proof must actually run git")
        for cmd, kwargs in spy.calls:
            self.assertEqual(cmd[0], "git")
            self.assertIn(
                _subcommand(cmd),
                shiki_sync_proof._READ_ONLY_GIT,
                f"non read-only git subcommand: {cmd}",
            )
            self.assertEqual(kwargs.get("cwd"), str(self.dir), f"cwd not target: {cmd}")

    def test_source_never_swallows_failures_or_imports_a_network_client(self) -> None:
        # Static half of the guard. The network *behaviour* is proven at runtime
        # by the read-only allowlist (below) and the orphan test, which observe
        # every subcommand actually spawned; here we forbid the two things a spy
        # cannot see: a swallowed failure and a smuggled network client import.
        src = Path(shiki_sync_proof.__file__).read_text(encoding="utf-8")
        self.assertNotIn("|| true", src)
        self.assertIsNone(re.search(r"except\s*:", src), "no bare except")
        self.assertIsNone(
            re.search(r"except[^\n]*:\s*(pass|continue)\b", src),
            "no except that silently continues",
        )
        self.assertIsNone(
            re.search(r"^\s*(import|from)\s+(urllib|socket|requests|http|ssl)\b", src, re.MULTILINE),
            "module must not import a network client",
        )
        # No command literal spawns the GitHub CLI (prose may still name it).
        self.assertNotIn('"gh"', src, "module must never shell out to gh")
        self.assertNotIn("'gh'", src, "module must never shell out to gh")

    def test_any_exception_is_named_at_the_boundary_not_proved(self) -> None:
        # A target that is not a git repository at all raises inside git; the
        # boundary must catch it and return proved=False with the failure named.
        with tempfile.TemporaryDirectory() as not_a_repo:
            proof = verify_pure_base_sync(
                target=not_a_repo, a_sha="deadbeef", head_sha="cafebabe", default_branch="main"
            )
        self.assertFalse(proof.proved)
        self.assertTrue(proof.reason, "the failure must be named")


class RegistrationTests(unittest.TestCase):
    def test_module_registered_in_template_paths(self) -> None:
        self.assertIn("scripts/shiki_sync_proof.py", shiki_installer.TEMPLATE_PATHS)

    def test_module_registered_in_module_boundaries(self) -> None:
        boundaries = (
            Path(shiki_test_support.SCRIPTS_DIR) / "test_shiki_module_boundaries.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("shiki_sync_proof", boundaries)


def _require_real_git() -> str:
    import shutil

    path = shutil.which("git")
    if path is None:  # pragma: no cover - git is a hard test prerequisite
        raise unittest.SkipTest("git not on PATH")
    return path


if __name__ == "__main__":
    unittest.main()
