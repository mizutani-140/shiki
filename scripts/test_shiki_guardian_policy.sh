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


def approved(*, pr=None, reviews=None, comments=None, events=None, test_policy=policy):
    result = evaluate_guardian_approval(
        policy=test_policy,
        pr=pr or base_pr,
        reviews=reviews or [],
        comments=comments or [],
        label_events=events if events is not None else label_events,
        head_sha=head,
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
]
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
