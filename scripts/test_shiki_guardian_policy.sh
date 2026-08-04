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
        # A negation that embeds the exact marker mid-sentence must not satisfy
        # the gate (line-initial requirement) and must be a recorded soft blocker.
        "negated marker blocks",
        approved(comments=[{"user": {"login": "mizutani-140"}, "body": f"No Guardian approval granted for {head}. This critical change is NOT authorized to merge."}]),
        False,
        "negates or revokes approval",
    ),
    (
        "withheld marker blocks",
        approved(comments=[{"user": {"login": "mizutani-140"}, "body": f"NOT approving. Guardian approval granted is withheld until CI is green for {head}."}]),
        False,
        "negates or revokes approval",
    ),
    (
        "revoked marker blocks",
        approved(comments=[{"user": {"login": "mizutani-140"}, "body": f"I am revoking my earlier approval. Guardian approval granted at {head} is RESCINDED."}]),
        False,
        "negates or revokes approval",
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

# Latest labeled/unlabeled transition governs label authority. A configured
# Guardian labeling that a later unlabel/relabel supersedes must not keep
# approving; the current label state (its most recent transition) decides.
_valid_comment = [{"user": {"login": "mizutani-140"}, "body": f"Guardian approval granted\n\n{head}"}]
relabel_by_mallory = approved(
    comments=_valid_comment,
    events=[
        {"event": "labeled", "label": {"name": "guardian:approved"}, "actor": {"login": "mizutani-140"}},
        {"event": "unlabeled", "label": {"name": "guardian:approved"}, "actor": {"login": "mizutani-140"}},
        {"event": "labeled", "label": {"name": "guardian:approved"}, "actor": {"login": "mallory"}},
    ],
)
if relabel_by_mallory.approved or not any("was not applied by a configured Guardian" in b for b in relabel_by_mallory.blockers):
    raise SystemExit(f"latest non-Guardian relabel must block: {relabel_by_mallory}")

relabel_by_guardian = approved(
    comments=_valid_comment,
    events=[
        {"event": "labeled", "label": {"name": "guardian:approved"}, "actor": {"login": "mallory"}},
        {"event": "unlabeled", "label": {"name": "guardian:approved"}, "actor": {"login": "mallory"}},
        {"event": "labeled", "label": {"name": "guardian:approved"}, "actor": {"login": "mizutani-140"}},
    ],
)
if not relabel_by_guardian.approved:
    raise SystemExit(f"latest Guardian relabel should approve: {relabel_by_guardian}")

final_unlabel = approved(
    comments=_valid_comment,
    events=[
        {"event": "labeled", "label": {"name": "guardian:approved"}, "actor": {"login": "mizutani-140"}},
        {"event": "unlabeled", "label": {"name": "guardian:approved"}, "actor": {"login": "mizutani-140"}},
    ],
)
if final_unlabel.approved:
    raise SystemExit(f"a revoked (final unlabel) label must not approve: {final_unlabel}")

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

# Defect 1 (pagination): EVERY Guardian-evidence `gh api` call that builds the
# live-guardian-{comments,events,timeline}.json evidence in BOTH the MergeGate
# and CCA workflows MUST page with --paginate. GitHub's issue-comments API pages
# at 30 by default, so without it a Contract PR with more than 30 comments cannot
# see an approval — or a REVOCATION — posted later than the 30th comment. Assert
# the two workflows cannot silently drift apart.
for wf in .github/workflows/shiki-mergegate.yml .github/workflows/shiki-cca-completion.yml; do
  for endpoint in comments events timeline; do
    ge_lines="$(grep -E "gh api .*issues/[^\"]*/${endpoint}\"" "$wf" || true)"
    if [ -z "$ge_lines" ]; then
      echo "no Guardian-evidence gh api ${endpoint} call found in ${wf}" >&2
      exit 1
    fi
    while IFS= read -r ge_line; do
      [ -z "$ge_line" ] && continue
      case "$ge_line" in
        *"gh api --paginate "*) : ;;
        *) echo "Guardian-evidence gh api ${endpoint} call missing --paginate in ${wf}: ${ge_line}" >&2; exit 1 ;;
      esac
    done <<EOF
$ge_lines
EOF
  done
done

# Defect 4 (PATCH target): the guardian-status comment the CCA workflow updates
# in place must be selected by AUTHOR — only github-actions[bot]'s OWN marker
# comment may be PATCHed, never a human comment that merely quotes the marker.
# When no bot comment exists it still posts a new one (the `else` branch below).
grep -Eq 'contains\("<!-- shiki:guardian-status -->"\).*\.user\.login.*github-actions\[bot\]' .github/workflows/shiki-cca-completion.yml \
  || { echo "guardian-status PATCH must select only github-actions[bot] comments" >&2; exit 1; }

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
    "body": "Guardian approval granted\n\naaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
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

# --------------------------------------------------------------------------- #
# `shiki guardian status`: show the Guardian what the gate is waiting for.
# Read-only advisory over the SAME evaluate_guardian_approval; it must never
# apply a label or post an approval. Driven offline (no gh/network) so it runs
# as ordinary coverage.
# --------------------------------------------------------------------------- #
GS="$TMP_ROOT/guardian-status"
mkdir -p "$GS"
HEAD_SHA="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
cat >"$GS/pr.json" <<JSON
{"number":4242,"title":"x","body":"T-0001","author":{"login":"mizutani-140"},"headRefName":"shiki/t","baseRefName":"main","headRefOid":"$HEAD_SHA","labels":[{"name":"guardian:approved"}],"reviews":[]}
JSON
cat >"$GS/events.json" <<'JSON'
[{"event":"labeled","label":{"name":"guardian:approved"},"actor":{"login":"mizutani-140"}}]
JSON
printf '[]\n' >"$GS/comments-empty.json"
cat >"$GS/comments-approved.json" <<JSON
[{"user":{"login":"mizutani-140"},"body":"Guardian approval granted\n\n$HEAD_SHA"}]
JSON

# Label present, no current-head comment: report the label satisfied, name the
# missing comment, and print a paste-ready body carrying the marker and the FULL
# 40-character head SHA.
python3 scripts/shiki.py guardian status --pr 4242 --repo mizutani-140/shiki --no-git \
  --guardian-policy .shiki/guardian-policy.json \
  --pr-json "$GS/pr.json" --comments "$GS/comments-empty.json" --events "$GS/events.json" \
  --output "$GS/label-only.txt" >/dev/null
grep -q "Result: NOT APPROVED" "$GS/label-only.txt"
grep -q "guardian_label" "$GS/label-only.txt"
grep -q "current-head Guardian comment" "$GS/label-only.txt"
grep -q "Guardian approval granted" "$GS/label-only.txt"
grep -q "$HEAD_SHA" "$GS/label-only.txt"

# Label + current-head Guardian comment: approved, approver named.
python3 scripts/shiki.py guardian status --pr 4242 --repo mizutani-140/shiki --no-git \
  --guardian-policy .shiki/guardian-policy.json \
  --pr-json "$GS/pr.json" --comments "$GS/comments-approved.json" --events "$GS/events.json" \
  --output "$GS/approved.txt" >/dev/null
grep -q "Result: APPROVED" "$GS/approved.txt"
grep -q "mizutani-140" "$GS/approved.txt"

# Comment format for the CCA-posted PR comment: carries the idempotency marker,
# the blockers, and the paste-ready body.
python3 scripts/shiki.py guardian status --pr 4242 --repo mizutani-140/shiki --no-git --format comment \
  --guardian-policy .shiki/guardian-policy.json \
  --pr-json "$GS/pr.json" --comments "$GS/comments-empty.json" --events "$GS/events.json" \
  --output "$GS/comment.md" >/dev/null
grep -q "<!-- shiki:guardian-status -->" "$GS/comment.md"
grep -q "What the gate is waiting for" "$GS/comment.md"
grep -q "$HEAD_SHA" "$GS/comment.md"

# The command is READ-ONLY: it must never apply a label, approve, merge, or post.
if grep -Eq "gh pr (review|comment|merge|edit)|--add-label|--approve" scripts/shiki_guardian_status.py; then
  echo "shiki_guardian_status.py must never apply a label or post an approval" >&2
  exit 1
fi

# The CCA workflow posts the same blockers as a PR comment when it returns
# needs_guardian, so they reach the approver on GitHub, not only the run log.
grep -q "Post Guardian blockers comment" .github/workflows/shiki-cca-completion.yml
grep -q 'verdict" != "needs_guardian"' .github/workflows/shiki-cca-completion.yml
grep -q "guardian status" .github/workflows/shiki-cca-completion.yml
grep -q -- "--format comment" .github/workflows/shiki-cca-completion.yml
grep -q "shiki:guardian-status" .github/workflows/shiki-cca-completion.yml

# The new module must stage into a fresh target so the target's own validate_shiki
# (which imports shiki_cli -> shiki_guardian_status) passes; otherwise the target
# fails with 'No module named shiki_guardian_status'.
GST="$TMP_ROOT/guardian-status-target"
mkdir -p "$GST"
python3 scripts/shiki.py install-target "$GST" --local-only >/dev/null
test -f "$GST/scripts/shiki_guardian_status.py"
(cd "$GST" && python3 scripts/validate_shiki.py >/dev/null)

python3 scripts/shiki.py doctor --json --target . >/tmp/shiki-guardian-doctor.json
grep '"id": "doctor.guardian.policy"' /tmp/shiki-guardian-doctor.json >/dev/null
grep '"id": "doctor.guardian.approvers"' /tmp/shiki-guardian-doctor.json >/dev/null

# --------------------------------------------------------------------------- #
# ADR 0015 Contract Approval (end-to-end): a contract-approved implementation PR
# with NO guardian:approved label and NO approval comment must resolve the
# Guardian requirement satisfied in BOTH the MergeGate result AND the CCA Guardian
# signal — and removing any single proof element must put it back to unsatisfied.
# Drives the REAL entry points (mergegate_check.py and guardian_approval_signal),
# never reads source.
# --------------------------------------------------------------------------- #
python3 - <<'PY'
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path.cwd()
sys.path.insert(0, str(root / "scripts"))
import guardian_approval_signal as sig

TASK = "T-7777"
GOAL = "G-0012"
HEAD = "a" * 40
IMPL_PR = 99   # implementation PR (carries no approval of its own)
REG_PR = 88    # the SEPARATE merged, Guardian-approved Contract PR


def valid_registration(**overrides):
    reg = {
        "task_id": TASK,
        "pr": REG_PR,
        "merged": True,
        "adding_commit": "c" * 40,
        "adding_commit_pr": REG_PR,
        "guardian_approved": True,
        "guardian_source": "guardian_label",
    }
    reg.update(overrides)
    return reg


TASK_DATA = {
    "id": TASK,
    "goal_id": GOAL,
    "status": "review",
    "risk_level": "critical",
    "scope": "add the contract-approval evaluator",
    "non_goals": ["no change to evaluate_guardian_approval"],
    "required_skills": [],
    "locks": ["scripts/**"],
    "acceptance_checks": ["a contract-approved PR needs no live approval"],
    "test_command": "python3 -m unittest discover -s tests",
    "dependencies": [],
    "expected_pr": IMPL_PR,
    "expected_branch": "shiki/t-7777",
    "ledger_evidence": ["L-7777"],
}

with tempfile.TemporaryDirectory() as tmp:
    ca = Path(tmp) / "ca"
    (ca / ".shiki" / "tasks").mkdir(parents=True)
    (ca / ".shiki" / "goals").mkdir()
    (ca / ".shiki" / "ledger").mkdir()
    (ca / ".shiki" / "gha").mkdir()
    for name in ("config.yaml", "manifest.json", "guardian-policy.json"):
        (ca / ".shiki" / name).write_text((root / ".shiki" / name).read_text(encoding="utf-8"), encoding="utf-8")
    (ca / ".shiki" / "goals" / f"{GOAL}.json").write_text(json.dumps({"id": GOAL, "status": "planned"}), encoding="utf-8")
    (ca / ".shiki" / "tasks" / f"{TASK}.json").write_text(json.dumps(TASK_DATA), encoding="utf-8")
    (ca / ".shiki" / "ledger" / "L-7777.json").write_text(
        json.dumps({
            "id": "L-7777", "goal_id": GOAL, "task_id": TASK, "type": "check",
            "summary": "PR #99 evidence", "evidence": ["PR #99", "/pull/99"],
            "links": ["https://github.com/example/shiki/pull/99"],
        }),
        encoding="utf-8",
    )
    # Base snapshot (.shiki dir) carrying the identical, already-registered contract.
    base_shiki = Path(tmp) / "base" / ".shiki"
    (base_shiki / "tasks").mkdir(parents=True)
    (base_shiki / "tasks" / f"{TASK}.json").write_text(json.dumps(TASK_DATA), encoding="utf-8")

    pr_json = ca / ".shiki" / "gha" / "pr.json"
    # NO guardian:approved label, NO approval comment: the only thing that can
    # satisfy the Guardian gate is Contract Approval.
    pr_json.write_text(
        json.dumps({
            "number": IMPL_PR,
            "body": f"{TASK}\n{GOAL}\n\n## Scope\nx\n\n## Acceptance\nx\n\n## Evidence\nx\n\n## MergeGate\nx",
            "author": {"login": "implementer"},
            "headRefName": "shiki/t-7777",
            "headRefOid": HEAD,
            "labels": [{"name": "risk:critical"}],
            "reviews": [],
        }),
        encoding="utf-8",
    )
    cca = ca / ".shiki" / "gha" / "cca-verdict.json"
    cca.write_text(
        json.dumps({
            "verdict": "complete", "summary": "fixture", "goal_id": GOAL, "task_id": TASK,
            "pr": IMPL_PR, "head_sha": HEAD, "can_merge": True, "checklist": [],
            "acceptance": [{"criterion": "fixture", "status": "pass", "evidence": ["fixture"]}],
            "mergegate": {}, "confidence": 1,
        }),
        encoding="utf-8",
    )
    cf = ca / ".shiki" / "gha" / "changed-files.txt"
    cfs = ca / ".shiki" / "gha" / "changed-files-status.txt"
    cf.write_text("", encoding="utf-8")
    cfs.write_text("", encoding="utf-8")
    comments = ca / ".shiki" / "gha" / "comments.json"
    events = ca / ".shiki" / "gha" / "events.json"
    comments.write_text("[]", encoding="utf-8")
    events.write_text("[]", encoding="utf-8")

    def write_registration(name, data):
        path = ca / ".shiki" / "gha" / name
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    valid_path = write_registration("contract-approval.json", valid_registration())

    def run_mergegate(contract_path):
        result_file = ca / ".shiki" / "gha" / "mergegate-result.json"
        cmd = [
            "python3", "scripts/mergegate_check.py", "--target", str(ca),
            "--pr-json", str(pr_json), "--cca-verdict", str(cca),
            "--changed-files", str(cf), "--changed-files-status", str(cfs),
            "--base-shiki", str(base_shiki), "--result-file", str(result_file),
            "--guardian-policy", ".shiki/guardian-policy.json",
            "--guardian-comments", str(comments), "--guardian-events", str(events),
            "--guardian-timeline", "",
        ]
        if contract_path is not None:
            cmd += ["--contract-approval", str(contract_path)]
        subprocess.run(cmd, check=False, capture_output=True)
        return json.loads(result_file.read_text(encoding="utf-8"))

    def run_signal(contract_path):
        out = ca / ".shiki" / "gha" / "signal.json"
        argv = [
            "--pr-json", str(pr_json), "--guardian-policy", str(ca / ".shiki" / "guardian-policy.json"),
            "--guardian-comments", str(comments), "--expected-repository", "example/shiki",
            "--shiki-root", str(ca), "--base-shiki", str(base_shiki), "--changed-files-status", str(cfs),
            "--output", str(out),
        ]
        if contract_path is not None:
            argv += ["--contract-approval", str(contract_path)]
        rc = sig.main_with_argv(argv)
        if rc != 0:
            raise SystemExit(f"guardian signal exited {rc}")
        return json.loads(out.read_text(encoding="utf-8"))

    def guardian_blocked(result):
        return any("guardian" in reason.lower() for reason in result.get("blocking_reasons") or [])

    # --- positive: the carry satisfies BOTH gates with no live approval --------
    mg = run_mergegate(valid_path)
    if guardian_blocked(mg):
        raise SystemExit(f"contract-approved PR must satisfy the MergeGate Guardian requirement: {mg['blocking_reasons']}")
    if not any("Contract Approval" in w for w in mg.get("warnings") or []):
        raise SystemExit(f"MergeGate must record the Contract Approval source: {mg.get('warnings')}")

    sg = run_signal(valid_path)
    if not (sg.get("required") and sg.get("approved") and "contract_approval" in (sg.get("sources") or [])):
        raise SystemExit(f"CCA Guardian signal must report contract_approval satisfied: {sg}")

    # --- removing ANY single proof element puts BOTH gates back to unsatisfied -
    negatives = {
        "merged=false": valid_registration(merged=False),
        "guardian_approved=false": valid_registration(guardian_approved=False),
        "pr/adding-commit mismatch": valid_registration(adding_commit_pr=REG_PR + 1),
        "task_id mismatch": valid_registration(task_id="T-9999"),
        "no guardian source": valid_registration(guardian_source=""),
        "not merged, not approved": valid_registration(merged=False, guardian_approved=False),
    }
    for label, reg in negatives.items():
        path = write_registration("contract-approval-neg.json", reg)
        mg_neg = run_mergegate(path)
        if not guardian_blocked(mg_neg):
            raise SystemExit(f"removing proof ({label}) must re-require Guardian in MergeGate: {mg_neg['blocking_reasons']}")
        sg_neg = run_signal(path)
        if sg_neg.get("approved"):
            raise SystemExit(f"removing proof ({label}) must re-require Guardian in the CCA signal: {sg_neg}")

    # --- flag absent / unreadable proof: byte-for-byte prior behaviour ---------
    for label, contract_path in (
        ("flag absent", None),
        ("unreadable proof", write_registration("contract-approval-bad.json", "{ this is not valid json")),
    ):
        mg_none = run_mergegate(contract_path)
        if not guardian_blocked(mg_none):
            raise SystemExit(f"{label}: Guardian must still be required in MergeGate: {mg_none['blocking_reasons']}")
        sg_none = run_signal(contract_path)
        if sg_none.get("approved"):
            raise SystemExit(f"{label}: Guardian must still be required in the CCA signal: {sg_none}")

print("contract approval end-to-end carry fixtures passed")
PY

echo "shiki guardian policy tests passed"
