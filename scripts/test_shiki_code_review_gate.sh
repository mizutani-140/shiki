#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-code-review-gate-test-$$"
TARGET="$TMP_ROOT/target"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

json_get() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"
}

cd "$ROOT"

python3 scripts/validate_shiki.py

# The wrapper skill must exist and be staged into targets.
test -f skills/engineering/code-review/SKILL.md
python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "scripts"))
import shiki_installer
from validate_shiki import KNOWN_SKILLS

if "code-review" not in KNOWN_SKILLS:
    raise SystemExit("KNOWN_SKILLS omitted code-review")
if "skills/engineering" not in set(shiki_installer.TEMPLATE_PATHS):
    raise SystemExit("TEMPLATE_PATHS omitted skills/engineering")
if "scripts/test_shiki_code_review_gate.sh" not in set(shiki_installer.TEMPLATE_PATHS):
    raise SystemExit("TEMPLATE_PATHS omitted scripts/test_shiki_code_review_gate.sh")
PY

mkdir -p "$TARGET"
python3 scripts/shiki.py install-target "$TARGET" --local-only >/tmp/shiki-code-review-install.out
test -f "$TARGET/skills/engineering/code-review/SKILL.md"

cd "$TARGET"
git init -b main >/tmp/shiki-code-review-git-init.out
git remote add origin https://github.com/example/shiki-code-review-gate-test.git
git add .
git -c user.name="Shiki Test" -c user.email="shiki@example.test" commit -m "init" >/tmp/shiki-code-review-commit.out

# A task registered without explicit skills defaults to tdd + code-review.
cat >"$TMP_ROOT/plan.json" <<'JSON'
{
  "title": "Ship the pre-PR code-review gate",
  "outcome": "Every implementation task carries the code-review gate by default",
  "completion_conditions": ["Default task skills include code-review"],
  "non_goals": ["No CI changes"],
  "risk_level": "low",
  "required_skills": ["grill-with-docs", "tdd"],
  "grill_with_docs": {
    "status": "complete",
    "source": "CONTEXT.md",
    "decisions": ["code-review is a mandatory pre-PR implementer gate (ADR 0008)"]
  },
  "spec_freeze": {
    "status": "frozen",
    "approved_by": "operator",
    "source": "test fixture"
  },
  "tasks": [
    {
      "title": "Default-skill task",
      "scope": "Smallest slice registered without explicit required_skills",
      "acceptance_checks": ["Task carries the default skill set"],
      "locks": ["path:marker.txt"]
    }
  ]
}
JSON

python3 "$ROOT/scripts/shiki.py" plan ingest --target "$TARGET" --plan-file "$TMP_ROOT/plan.json" >/tmp/shiki-code-review-plan.json
PLAN_ID="$(json_get /tmp/shiki-code-review-plan.json plan_id)"
python3 "$ROOT/scripts/shiki.py" run --target "$TARGET" --plan "$PLAN_ID" >/tmp/shiki-code-review-run.json
python3 "$ROOT/scripts/shiki.py" runner next --target "$TARGET" >/tmp/shiki-code-review-next.json
TASK_ID="$(json_get /tmp/shiki-code-review-next.json task_id)"

python3 - "$TARGET/.shiki/tasks/$TASK_ID.json" <<'PY'
import json
import sys

task = json.load(open(sys.argv[1]))
skills = task.get("required_skills", [])
if skills != ["tdd", "code-review"]:
    raise SystemExit(f"default required_skills must be ['tdd', 'code-review'], got {skills}")
PY

# An explicit code-review requirement validates against the installed target.
python3 "$ROOT/scripts/shiki.py" issue plan \
  --target "$TARGET" \
  --goal-id "$(json_get /tmp/shiki-code-review-run.json goal_id)" \
  --title "Explicit code-review task" \
  --scope "Carries the gate explicitly" \
  --required-skill tdd --required-skill code-review \
  --acceptance-check "Gate evidence recorded" >/tmp/shiki-code-review-issue.json

# A repair packet may route the repair through the code-review skill.
python3 "$ROOT/scripts/shiki.py" repair packet \
  --target "$TARGET" \
  --task-id "$TASK_ID" \
  --pr 1 \
  --required-skill code-review \
  --minimal-change "Apply the pre-PR review findings" \
  --verification-command "python3 scripts/validate_shiki.py" >/tmp/shiki-code-review-repair.json

# The installed target's validator accepts all of the above state.
cd "$TARGET"
python3 "$TARGET/scripts/validate_shiki.py"

echo "shiki code-review gate tests passed"
