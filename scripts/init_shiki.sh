#!/usr/bin/env bash
set -euo pipefail

echo "[Shiki] Initializing .shiki workspace..."

# Core directories
mkdir -p .shiki/{tasks,contracts,reports,plans,state,schemas,dag}

# Claude agents directory
mkdir -p .claude/agents

# If empty, copy schemas from this kit (if present)
if [[ -f ".shiki/schemas/task.schema.json" ]]; then
  echo "[Shiki] Schemas already present."
else
  echo "[Shiki] Missing schemas. Did you copy the framework correctly?"
fi

# Seed PLAN if missing
if [[ ! -f ".shiki/plans/PLAN.md" && -f "templates/PLAN.template.md" ]]; then
  cp "templates/PLAN.template.md" ".shiki/plans/PLAN.md"
  echo "[Shiki] Seeded .shiki/plans/PLAN.md"
fi

# Copy agent definitions if not present
for agent in coordinator executor monitor; do
  if [[ -f ".claude/agents/${agent}.md" ]]; then
    echo "[Shiki] Agent ${agent}.md already present."
  else
    if [[ -f "templates/${agent}.agent.md" ]]; then
      cp "templates/${agent}.agent.md" ".claude/agents/${agent}.md"
      echo "[Shiki] Seeded .claude/agents/${agent}.md"
    fi
  fi
done

# Mode selection
echo ""
echo "[Shiki] Mode selection:"
echo "  1) CLI mode  (Agent Teams native + tmux/iTerm2)"
echo "  2) GitHub mode (Issue/Label/Worktree/DAG driven)"
echo "  3) Auto (detect from environment)"
echo ""

if [[ -t 0 ]]; then
  read -r -p "Select mode [3]: " MODE_CHOICE
  MODE_CHOICE=${MODE_CHOICE:-3}
else
  MODE_CHOICE=3
fi

case "$MODE_CHOICE" in
  1)
    echo "[Shiki] CLI mode selected."
    echo "[Shiki] Run: scripts/start_cli_session.sh to begin."
    ;;
  2)
    echo "[Shiki] GitHub mode selected."
    echo "[Shiki] Ensure GitHub Secrets are set:"
    echo "  Recommended: CLAUDE_CODE_OAUTH_TOKEN (Max plan OAuth, no API billing)"
    echo "  Alternative: ANTHROPIC_API_KEY (API key, pay-per-use)"
    echo "  Optional:    OPENAI_API_KEY (for Codex engine)"
    echo "[Shiki] Run: scripts/setup_github_labels.sh to create labels."
    ;;
  *)
    echo "[Shiki] Auto mode selected (default)."
    ;;
esac

echo ""
echo "[Shiki] Done."
echo ""
echo "Next steps:"
echo "  1) cp templates/GOAL.template.md GOAL.md"
echo "  2) Edit GOAL.md with your goal and acceptance criteria"
echo "  3) CLI:    scripts/start_cli_session.sh"
echo "     GitHub: Create an Issue with 'ai-goal' label"
