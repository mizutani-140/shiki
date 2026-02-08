#!/usr/bin/env bash
set -euo pipefail

echo "=========================================="
echo "  Shiki（式） Project Initializer"
echo "=========================================="
echo ""

# Core directories (ensure they exist with .keep files)
for dir in .shiki/{tasks,contracts,reports,plans,state,schemas,dag} .ai/logs .claude/agents; do
  mkdir -p "$dir"
  [[ -f "$dir/.keep" ]] || touch "$dir/.keep"
done

echo "[Shiki] Directory structure verified."

# Schema check
if [[ -f ".shiki/schemas/task.schema.json" ]]; then
  echo "[Shiki] Schemas present."
else
  echo "[Shiki] WARNING: Missing schemas. Ensure you copied/templated from the Shiki repository."
fi

# Seed PLAN if missing
if [[ ! -f ".shiki/plans/PLAN.md" && -f "templates/PLAN.template.md" ]]; then
  cp "templates/PLAN.template.md" ".shiki/plans/PLAN.md"
  echo "[Shiki] Seeded .shiki/plans/PLAN.md"
fi

# Copy agent definitions if not present
for agent in coordinator executor monitor; do
  if [[ ! -f ".claude/agents/${agent}.md" && -f "templates/${agent}.agent.md" ]]; then
    cp "templates/${agent}.agent.md" ".claude/agents/${agent}.md"
    echo "[Shiki] Seeded .claude/agents/${agent}.md"
  fi
done

# Clean template sample data (if this is a fresh template creation)
SAMPLE_TASK=".shiki/tasks/T-0001.json"
if [[ -f "$SAMPLE_TASK" ]]; then
  echo ""
  read -r -p "[Shiki] Sample task T-0001.json found. Remove it? [Y/n]: " REMOVE_SAMPLE
  REMOVE_SAMPLE=${REMOVE_SAMPLE:-Y}
  if [[ "$REMOVE_SAMPLE" =~ ^[Yy]$ ]]; then
    rm "$SAMPLE_TASK"
    echo "[Shiki] Removed sample task."
  fi
fi

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

# GOAL.md guidance
echo ""
if [[ -f "GOAL.md" ]]; then
  echo "[Shiki] GOAL.md exists. Edit it with your project's goal."
else
  if [[ -f "templates/GOAL.template.md" ]]; then
    cp "templates/GOAL.template.md" GOAL.md
    echo "[Shiki] Created GOAL.md from template."
  fi
fi

echo ""
echo "=========================================="
echo "  Setup complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "  1) Edit GOAL.md with your goal and acceptance criteria"
echo "  2) CLI:    ./scripts/start_cli_session.sh"
echo "     GitHub: Create an Issue with 'ai-goal' label"
echo "  3) See SETUP.md for detailed configuration"
echo ""
