#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-guardian-policy-test-$$"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

cd "$ROOT"

python3 - <<'PY'
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path.cwd() / "scripts"))

from shiki_guardian import evaluate_guardian_approval, load_guardian_policy, risk_requires_guardian, validate_guardian_policy

root = Path.cwd()
policy = load_guardian_policy(root)
errors = validate_guardian_policy(policy)
if errors:
    raise SystemExit(f"default policy should validate: {errors}")
if not risk_requires_guardian(["risk:critical"], policy):
    raise SystemExit("critical risk should require Guardian")
if not risk_requires_guardian(["high"], policy):
    raise SystemExit("high risk should require Guardian")
if risk_requires_guardian(["risk:low"], policy):
    raise SystemExit("low risk should not require Guardian")


def invalid_policy(**overrides):
    data = json.loads((root / ".shiki/guardian-policy.json").read_text(encoding="utf-8"))
    for key, value in overrides.items():
        target = data
        parts = key.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        (tmp_root / ".shiki").mkdir()
        (tmp_root / ".shiki/guardian-policy.json").write_text(json.dumps(data), encoding="utf-8")
        return validate_guardian_policy(load_guardian_policy(tmp_root))


fixtures = [
    ("empty approvers", {"approvers.users": [], "approvers.teams": []}, "at least one Guardian"),
    ("invalid risk", {"applies_to_risk": ["critical", "surprise"]}, "unsupported risk level"),
    ("empty label", {"approval_sources.guardian_label.label": ""}, "guardian label"),
    ("empty marker", {"approval_sources.guardian_comment.marker": ""}, "comment marker"),
    ("solo without rationale", {"solo_maintainer.rationale": ""}, "solo maintainer"),
    (
        "review bridge true",
        {"exclusions.github_actions_review_bridge_counts_as_guardian": True},
        "CCA Review Bridge",
    ),
]
for name, overrides, needle in fixtures:
    errors = invalid_policy(**overrides)
    if not any(needle in error for error in errors):
        raise SystemExit(f"{name} fixture did not fail with {needle!r}: {errors}")

head = "a" * 40
base_pr = {
    "number": 55,
    "headRefOid": head,
    "author": {"login": "mizutani-140"},
    "labels": [{"name": "guardian:approved"}, {"name": "risk:critical"}],
}
label_events = [{"event": "labeled", "label": {"name": "guardian:approved"}, "actor": {"login": "mizutani-140"}}]


def approved(*, pr=None, reviews=None, comments=None, events=None, test_policy=policy, expected_repo="mizutani-140/shiki"):
    result = evaluate_guardian_approval(
        policy=test_policy,
        pr=pr or base_pr,
        reviews=reviews or [],
        comments=comments or [],
        label_events=events if events is not None else label_events,
        head_sha=head,
        expected_repo=expected_repo,
    )
    return result


cases = [
    ("label alone blocks", approved(), False, "review or current-head"),
    (
        "non guardian comment blocks",
        approved(comments=[{"user": {"login": "someone-else"}, "body": f"Guardian approval granted {head}"}]),
        False,
        "not configured",
    ),
    (
        "guardian comment without head blocks",
        approved(comments=[{"user": {"login": "mizutani-140"}, "body": "Guardian approval granted"}]),
        False,
        "current head SHA",
    ),
    (
        "guardian comment with head passes",
        approved(comments=[{"user": {"login": "mizutani-140"}, "body": f"Guardian approval granted\n\n{head}"}]),
        True,
        "",
    ),
    (
        "guardian review passes",
        approved(reviews=[{"state": "APPROVED", "author": {"login": "mizutani-140"}}]),
        True,
        "",
    ),
    (
        "github actions review blocks",
        approved(reviews=[{"state": "APPROVED", "author": {"login": "github-actions[bot]"}}]),
        False,
        "review or current-head",
    ),
    (
        "claude review blocks",
        approved(reviews=[{"state": "APPROVED", "author": {"login": "claude-code-action"}}]),
        False,
        "review or current-head",
    ),
    (
        "negative text blocks",
        approved(comments=[{"user": {"login": "mizutani-140"}, "body": f"no Guardian approval evidence is present for {head}"}]),
        False,
        "review or current-head",
    ),
    (
        # B1(a): a configured-Guardian label plus an APPROVED review by an
        # arbitrary unconfigured reviewer must NOT satisfy the secondary human
        # review path; the stray review is explicitly fail-closed.
        "unconfigured review does not satisfy secondary path",
        approved(reviews=[{"state": "APPROVED", "author": {"login": "random-user"}}]),
        False,
        "review actor random-user is not configured",
    ),
]

_ai = (
    '```external-ai-guardian-review\n'
    '{"kind":"external_ai_guardian_review","reviewer":{"type":"ai_model","model":"GPT-5.5 Pro","role":"external_guardian_reviewer"},'
    '"repo":"mizutani-140/shiki","pr":55,"head_sha":"%s","verdict":"approve","merge_permission":"autonomous_merge_permitted","not_operator_approval":true}\n'
    '```'
) % head
# A PR WITHOUT the human guardian label, so the AI path is the only authority
# and identity preservation can be checked cleanly.
ai_pr = {"number": 55, "headRefOid": head, "author": {"login": "mizutani-140"}, "labels": [{"name": "risk:critical"}]}
# A second comment body carrying a malformed leading fence followed by the valid
# artifact — the parser must scan ALL fenced blocks, not just the first.
_ai_double = (
    '```external-ai-guardian-review\n{ this is not json }\n```\n\nand then:\n\n' + _ai
)
# An artifact bound to a different repository (cross-repo replay).
_ai_other_repo = _ai.replace('"repo":"mizutani-140/shiki"', '"repo":"attacker/evil"')
# An artifact bound to a different PR number.
_ai_other_pr = _ai.replace('"pr":55', '"pr":999')
# An artifact that falsely claims it is NOT distinct from operator approval.
_ai_claims_operator = _ai.replace('"not_operator_approval":true', '"not_operator_approval":false')
# Identity-boundary fields must fail closed when missing/null or wrong type.
_ai_missing_noa = _ai.replace(',"not_operator_approval":true', '')
_ai_null_noa = _ai.replace('"not_operator_approval":true', '"not_operator_approval":null')
_ai_missing_type = _ai.replace('"type":"ai_model",', '')
_ai_wrong_type = _ai.replace('"type":"ai_model"', '"type":"human"')
ai_cases = [
    # AI guardian review approves with no human label; identity preserved.
    ("ai review approves", approved(pr=ai_pr, events=[], comments=[{"user": {"login": "mizutani-140"}, "body": "External AI guardian review:\n" + _ai}]), True, ""),
    # Stale short-SHA comment alongside a valid AI artifact must NOT poison the gate.
    ("ai review survives stale comment", approved(pr=ai_pr, events=[], comments=[
        {"user": {"login": "mizutani-140"}, "body": "Guardian approval granted for head SHA dead."},
        {"user": {"login": "mizutani-140"}, "body": _ai},
    ]), True, ""),
    # A non-Guardian griefing comment must NOT block a valid AI approval.
    ("ai review survives griefing comment", approved(pr=ai_pr, events=[], comments=[
        {"user": {"login": "random-troll"}, "body": f"Guardian approval granted {head}"},
        {"user": {"login": "mizutani-140"}, "body": _ai},
    ]), True, ""),
    # The parser scans every fenced block; a malformed leading fence does not hide a valid one.
    ("ai review scans all fences", approved(pr=ai_pr, events=[], comments=[{"user": {"login": "mizutani-140"}, "body": _ai_double}]), True, ""),
    # Wrong head SHA in the AI artifact does not approve.
    ("ai review wrong head blocks", approved(pr=ai_pr, events=[], comments=[{"user": {"login": "mizutani-140"}, "body": _ai.replace(head, "0" * 40)}]), False, "current head SHA"),
    # AI artifact relayed by a non-Guardian does not approve.
    ("ai review non-guardian relay blocks", approved(pr=ai_pr, events=[], comments=[{"user": {"login": "someone-else"}, "body": _ai}]), False, "non-Guardian"),
    # Cross-repo replay: artifact bound to a different repository is rejected.
    ("ai review cross-repo replay blocks", approved(pr=ai_pr, events=[], comments=[{"user": {"login": "mizutani-140"}, "body": _ai_other_repo}]), False, "this repository"),
    # Cross-PR replay: artifact bound to a different PR is rejected.
    ("ai review cross-pr replay blocks", approved(pr=ai_pr, events=[], comments=[{"user": {"login": "mizutani-140"}, "body": _ai_other_pr}]), False, "this PR"),
    # Fail closed when the evaluator is given no expected repository to bind to.
    ("ai review no expected repo fails closed", approved(pr=ai_pr, events=[], comments=[{"user": {"login": "mizutani-140"}, "body": _ai}], expected_repo=""), False, "this repository"),
    # An artifact explicitly claiming it IS operator approval must not take the
    # AI path (folds into the same fail-closed not_operator_approval=true check).
    ("ai review claiming operator approval blocks", approved(pr=ai_pr, events=[], comments=[{"user": {"login": "mizutani-140"}, "body": _ai_claims_operator}]), False, "not_operator_approval=true"),
    # B2 identity boundary: missing/null not_operator_approval fails closed.
    ("ai review missing not_operator_approval blocks", approved(pr=ai_pr, events=[], comments=[{"user": {"login": "mizutani-140"}, "body": _ai_missing_noa}]), False, "not_operator_approval=true"),
    ("ai review null not_operator_approval blocks", approved(pr=ai_pr, events=[], comments=[{"user": {"login": "mizutani-140"}, "body": _ai_null_noa}]), False, "not_operator_approval=true"),
    # B2 identity boundary: reviewer.type must explicitly be ai_model.
    ("ai review missing reviewer type blocks", approved(pr=ai_pr, events=[], comments=[{"user": {"login": "mizutani-140"}, "body": _ai_missing_type}]), False, "reviewer.type must be ai_model"),
    ("ai review non-ai_model reviewer blocks", approved(pr=ai_pr, events=[], comments=[{"user": {"login": "mizutani-140"}, "body": _ai_wrong_type}]), False, "reviewer.type must be ai_model"),
    # B1(b): a stray unconfigured GitHub review must NOT poison a valid AI approval.
    ("ai review survives unconfigured stray review", approved(pr=ai_pr, events=[],
        reviews=[{"state": "APPROVED", "author": {"login": "random-user"}}],
        comments=[{"user": {"login": "mizutani-140"}, "body": _ai}]), True, ""),
]
for name, result, expected, needle in ai_cases:
    if result.approved is not expected:
        raise SystemExit(f"{name}: expected approved={expected}, got {result}")
    if expected:
        if "external_ai_guardian_review" not in result.sources:
            raise SystemExit(f"{name}: external_ai_guardian_review missing from sources {result.sources}")
        if "GPT-5.5 Pro" not in result.ai_reviewers:
            raise SystemExit(f"{name}: AI reviewer identity not recorded: {result.ai_reviewers}")
        if "mizutani-140" in result.approvers:
            raise SystemExit(f"{name}: human relay must NOT be recorded as approver: {result.approvers}")
    elif needle and not any(needle in msg for msg in (result.blockers + result.warnings)):
        raise SystemExit(f"{name}: missing rejection reason {needle!r}: blockers={result.blockers} warnings={result.warnings}")

for name, result, expected, needle in cases:
    if result.approved is not expected:
        raise SystemExit(f"{name}: expected approved={expected}, got {result}")
    if needle and not any(needle in blocker for blocker in result.blockers):
        raise SystemExit(f"{name}: missing blocker {needle!r}: {result.blockers}")

disabled_errors = invalid_policy(**{"solo_maintainer.enabled": False, "solo_maintainer.allow_pr_author_as_guardian": False})
if disabled_errors:
    raise SystemExit(f"solo disabled policy should validate: {disabled_errors}")

with tempfile.TemporaryDirectory() as tmp:
    tmp_root = Path(tmp)
    data = json.loads((root / ".shiki/guardian-policy.json").read_text(encoding="utf-8"))
    data["solo_maintainer"]["enabled"] = False
    data["solo_maintainer"]["allow_pr_author_as_guardian"] = False
    (tmp_root / ".shiki").mkdir()
    (tmp_root / ".shiki/guardian-policy.json").write_text(json.dumps(data), encoding="utf-8")
    no_solo_policy = load_guardian_policy(tmp_root)
    result = approved(
        comments=[{"user": {"login": "mizutani-140"}, "body": f"Guardian approval granted {head}"}],
        test_policy=no_solo_policy,
    )
    if result.approved or not any("PR author" in blocker for blocker in result.blockers):
        raise SystemExit(f"PR author should block when solo mode is disabled: {result}")

    # BLOCKER regression: the external AI guardian path must be NO WEAKER than
    # the human comment path. When solo-maintainer is disabled, a PR author who
    # relays their own AI artifact must NOT be able to self-approve their own
    # critical PR — exactly the PR-author guard every human path enforces.
    ai_self_relay = approved(
        pr=ai_pr,
        events=[],
        comments=[{"user": {"login": "mizutani-140"}, "body": _ai}],
        test_policy=no_solo_policy,
    )
    if ai_self_relay.approved:
        raise SystemExit(f"PR author self-relayed AI artifact must NOT approve when solo disabled: {ai_self_relay}")
    if "external_ai_guardian_review" in ai_self_relay.sources:
        raise SystemExit(f"PR-author self-relay must not register an AI approval source: {ai_self_relay}")
    if not any("PR author" in msg for msg in (ai_self_relay.blockers + ai_self_relay.warnings)):
        raise SystemExit(f"PR-author self-relay rejection must explain the PR-author guard: {ai_self_relay}")

with tempfile.TemporaryDirectory() as tmp2:
    # MAJOR regression (review-path poisoning): the poisoning fix must be
    # symmetric across ALL human paths. With solo disabled and TWO configured
    # guardians, a VALID external AI guardian review relayed by the second
    # guardian must survive a stray APPROVED GitHub review left by the PR author
    # (whose own review cannot satisfy approval). Before the fix, the PR-author
    # review produced a HARD review-path blocker that poisoned the valid AI
    # approval; now it is a soft signal demoted to a warning.
    tmp2_root = Path(tmp2)
    data2 = json.loads((root / ".shiki/guardian-policy.json").read_text(encoding="utf-8"))
    data2["solo_maintainer"]["enabled"] = False
    data2["solo_maintainer"]["allow_pr_author_as_guardian"] = False
    data2["approvers"]["users"] = ["mizutani-140", "second-guardian"]
    (tmp2_root / ".shiki").mkdir()
    (tmp2_root / ".shiki/guardian-policy.json").write_text(json.dumps(data2), encoding="utf-8")
    two_guardian_policy = load_guardian_policy(tmp2_root)

    # Baseline: AI artifact relayed by the second (non-author) guardian approves.
    ai_relayed = approved(
        pr=ai_pr,
        events=[],
        comments=[{"user": {"login": "second-guardian"}, "body": _ai}],
        test_policy=two_guardian_policy,
    )
    if not ai_relayed.approved or "external_ai_guardian_review" not in ai_relayed.sources:
        raise SystemExit(f"AI artifact relayed by a second guardian should approve under solo disabled: {ai_relayed}")

    # The stray PR-author APPROVED review must NOT poison that valid AI approval.
    survives_review = approved(
        pr=ai_pr,
        events=[],
        reviews=[{"state": "APPROVED", "author": {"login": "mizutani-140"}}],
        comments=[{"user": {"login": "second-guardian"}, "body": _ai}],
        test_policy=two_guardian_policy,
    )
    if not survives_review.approved:
        raise SystemExit(f"stray PR-author review must NOT poison a valid AI approval: {survives_review}")
    if "mizutani-140" in survives_review.approvers:
        raise SystemExit(f"stray PR-author review must not be recorded as an approver: {survives_review}")

print("guardian policy evaluator fixtures passed")
PY

grep "live-guardian-comments.json" .github/workflows/shiki-cca-completion.yml >/dev/null
grep "live-guardian-events.json" .github/workflows/shiki-cca-completion.yml >/dev/null
grep -- "--guardian-comments .shiki/gha/live-guardian-comments.json" .github/workflows/shiki-cca-completion.yml >/dev/null
grep -- "--guardian-events .shiki/gha/live-guardian-events.json" .github/workflows/shiki-cca-completion.yml >/dev/null

MG="$TMP_ROOT/mergegate"
mkdir -p "$MG/.shiki/tasks" "$MG/.shiki/goals" "$MG/.shiki/ledger" "$MG/.shiki/gha" "$MG/.github/workflows"
cp .shiki/config.yaml "$MG/.shiki/config.yaml"
cp .shiki/manifest.json "$MG/.shiki/manifest.json"
cp .shiki/guardian-policy.json "$MG/.shiki/guardian-policy.json"
cp -R .shiki/schemas "$MG/.shiki/schemas"
cp .github/workflows/shiki-validate.yml "$MG/.github/workflows/shiki-validate.yml"
cp .github/workflows/shiki-cca-completion.yml "$MG/.github/workflows/shiki-cca-completion.yml"
cp .github/workflows/shiki-mergegate.yml "$MG/.github/workflows/shiki-mergegate.yml"
cp .github/workflows/shiki-claude-review.yml "$MG/.github/workflows/shiki-claude-review.yml"
cp .github/workflows/shiki-orchestrator.yml "$MG/.github/workflows/shiki-orchestrator.yml"
cat >"$MG/.shiki/goals/G-0012.json" <<'JSON'
{"id":"G-0012","status":"planned"}
JSON
cat >"$MG/.shiki/tasks/T-9999.json" <<'JSON'
{
  "id": "T-9999",
  "goal_id": "G-0012",
  "status": "review",
  "risk_level": "critical",
  "locks": ["scripts/**"],
  "ledger_evidence": ["L-9999"],
  "required_skills": []
}
JSON
cat >"$MG/.shiki/ledger/L-9999.json" <<'JSON'
{
  "id": "L-9999",
  "goal_id": "G-0012",
  "task_id": "T-9999",
  "type": "check",
  "summary": "PR #99 evidence",
  "evidence": ["PR #99"],
  "links": ["https://github.com/example/shiki/pull/99"]
}
JSON
cat >"$MG/.shiki/gha/live-pr.json" <<'JSON'
{
  "number": 99,
  "body": "T-9999\nG-0012\n\n## Scope\nx\n\n## Acceptance\nx\n\n## Evidence\nx\n\n## MergeGate\nx",
  "author": {"login": "mizutani-140"},
  "headRefName": "shiki/t-9999",
  "headRefOid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "labels": [{"name": "risk:critical"}, {"name": "guardian:approved"}],
  "reviews": [{"state": "APPROVED", "author": {"login": "github-actions[bot]"}}],
  "reviewDecision": "APPROVED",
  "statusCheckRollup": [
    {"name":"Validate Shiki mirror","status":"COMPLETED","conclusion":"SUCCESS","headSha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    {"name":"MergeGate metadata check","status":"COMPLETED","conclusion":"SUCCESS","headSha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    {"name":"Claude review","status":"COMPLETED","conclusion":"SUCCESS","headSha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
  ]
}
JSON
cat >"$MG/.shiki/gha/cca-verdict.json" <<'JSON'
{
  "verdict": "complete",
  "summary": "fixture",
  "goal_id": "G-0012",
  "task_id": "T-9999",
  "pr": 99,
  "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "can_merge": true,
  "checklist": [],
  "acceptance": [{"criterion":"fixture","status":"pass","evidence":["fixture"]}],
  "mergegate": {},
  "confidence": 1
}
JSON
cp "$MG/.shiki/gha/live-pr.json" "$MG/.shiki/gha/pr.json"
touch "$MG/.shiki/gha/live-changed-files.txt" "$MG/.shiki/gha/live-changed-files-status.txt"
touch "$MG/.shiki/gha/changed-files.txt" "$MG/.shiki/gha/changed-files-status.txt"
python3 scripts/build_cca_evidence_manifest.py \
  --repo example/shiki \
  --pr 99 \
  --head-sha aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --workflow-name "Shiki CCA Completion" \
  --run-id 123 \
  --run-attempt 1 \
  --event-name pull_request \
  --artifact-name shiki-cca-evidence \
  --evidence-dir "$MG/.shiki/gha" \
  --output "$MG/.shiki/gha/cca-evidence-manifest.json" >/dev/null

if python3 scripts/mergegate_check.py --target "$MG" --pr-json "$MG/.shiki/gha/live-pr.json" --cca-verdict "$MG/.shiki/gha/cca-verdict.json" --cca-evidence-manifest "$MG/.shiki/gha/cca-evidence-manifest.json" --expected-repository example/shiki --changed-files "$MG/.shiki/gha/live-changed-files.txt" --changed-files-status "$MG/.shiki/gha/live-changed-files-status.txt" --result-file "$MG/.shiki/gha/mergegate-result.json" --guardian-policy .shiki/guardian-policy.json --guardian-comments .shiki/gha/missing-comments.json --guardian-events .shiki/gha/missing-events.json --guardian-timeline .shiki/gha/missing-timeline.json >/tmp/shiki-guardian-mergegate-missing.out 2>&1; then
  echo "MergeGate should block high-risk PR when Guardian evidence files are missing" >&2
  exit 1
fi
grep "Guardian comments evidence file is missing" /tmp/shiki-guardian-mergegate-missing.out >/dev/null

cat >"$MG/.shiki/gha/live-guardian-comments.json" <<'JSON'
[
  {
    "user": {"login": "mizutani-140"},
    "body": "Guardian approval granted for head aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
]
JSON
cat >"$MG/.shiki/gha/live-guardian-events.json" <<'JSON'
[
  {
    "event": "labeled",
    "label": {"name": "guardian:approved"},
    "actor": {"login": "mizutani-140"}
  }
]
JSON
printf '[]\n' >"$MG/.shiki/gha/live-guardian-timeline.json"

python3 scripts/mergegate_check.py --target "$MG" --pr-json "$MG/.shiki/gha/live-pr.json" --cca-verdict "$MG/.shiki/gha/cca-verdict.json" --cca-evidence-manifest "$MG/.shiki/gha/cca-evidence-manifest.json" --expected-repository example/shiki --changed-files "$MG/.shiki/gha/live-changed-files.txt" --changed-files-status "$MG/.shiki/gha/live-changed-files-status.txt" --result-file "$MG/.shiki/gha/mergegate-result.json" --guardian-policy .shiki/guardian-policy.json --guardian-comments .shiki/gha/live-guardian-comments.json --guardian-events .shiki/gha/live-guardian-events.json --guardian-timeline .shiki/gha/live-guardian-timeline.json >/tmp/shiki-guardian-mergegate-pass.out
grep '"mergegate": "ready"' /tmp/shiki-guardian-mergegate-pass.out >/dev/null

python3 scripts/shiki.py doctor --json --target . >/tmp/shiki-guardian-doctor.json
grep '"id": "doctor.guardian.policy"' /tmp/shiki-guardian-doctor.json >/dev/null
grep '"id": "doctor.guardian.approvers"' /tmp/shiki-guardian-doctor.json >/dev/null

echo "shiki guardian policy tests passed"
