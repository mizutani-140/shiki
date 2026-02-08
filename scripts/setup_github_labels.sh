#!/usr/bin/env bash
# =============================================================================
# setup_github_labels.sh
# GitHub リポジトリに .github/labels.json で定義されたラベルを作成・更新する
#
# Usage:
#   bash scripts/setup_github_labels.sh [--force] [--dry-run]
#
# Options:
#   --force    既存ラベルを上書き更新する
#   --dry-run  実際には作成せず、実行予定の操作を表示する
#
# Requirements:
#   - gh CLI (authenticated)
#   - jq
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LABELS_FILE="$REPO_ROOT/.github/labels.json"

# --- Parse arguments ---
FORCE=false
DRY_RUN=false

for arg in "$@"; do
  case "$arg" in
    --force)
      FORCE=true
      ;;
    --dry-run)
      DRY_RUN=true
      ;;
    --help|-h)
      echo "Usage: bash scripts/setup_github_labels.sh [--force] [--dry-run]"
      echo ""
      echo "Options:"
      echo "  --force    既存ラベルを上書き更新する"
      echo "  --dry-run  実際には作成せず、実行予定の操作を表示する"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

# --- Validate prerequisites ---
if ! command -v gh &>/dev/null; then
  echo "[ERROR] gh CLI is not installed. Install from: https://cli.github.com/" >&2
  exit 1
fi

if ! command -v jq &>/dev/null; then
  echo "[ERROR] jq is not installed. Install with: brew install jq (macOS) or apt install jq (Linux)" >&2
  exit 1
fi

if ! gh auth status &>/dev/null; then
  echo "[ERROR] gh CLI is not authenticated. Run: gh auth login" >&2
  exit 1
fi

if [ ! -f "$LABELS_FILE" ]; then
  echo "[ERROR] Labels file not found: $LABELS_FILE" >&2
  exit 1
fi

# --- Get current repository ---
REPO=$(gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null || echo "")
if [ -z "$REPO" ]; then
  echo "[ERROR] Could not determine repository. Are you in a git repo with a GitHub remote?" >&2
  exit 1
fi

echo "[Shiki] Setting up labels for: $REPO"
echo "[Shiki] Labels file: $LABELS_FILE"
echo "[Shiki] Force: $FORCE | Dry run: $DRY_RUN"
echo ""

# --- Fetch existing labels ---
echo "[Shiki] Fetching existing labels..."
EXISTING_LABELS=$(gh label list --repo "$REPO" --json name --limit 200 -q '.[].name' 2>/dev/null || echo "")

# --- Counters ---
CREATED=0
UPDATED=0
SKIPPED=0
ERRORS=0

# --- Process each label ---
LABEL_COUNT=$(jq length "$LABELS_FILE")
echo "[Shiki] Processing $LABEL_COUNT labels..."
echo ""

for i in $(seq 0 $(( LABEL_COUNT - 1 ))); do
  NAME=$(jq -r ".[$i].name" "$LABELS_FILE")
  COLOR=$(jq -r ".[$i].color" "$LABELS_FILE")
  DESCRIPTION=$(jq -r ".[$i].description" "$LABELS_FILE")

  # Check if label exists
  EXISTS=false
  while IFS= read -r existing; do
    if [ "$existing" = "$NAME" ]; then
      EXISTS=true
      break
    fi
  done <<< "$EXISTING_LABELS"

  if [ "$EXISTS" = true ]; then
    if [ "$FORCE" = true ]; then
      if [ "$DRY_RUN" = true ]; then
        echo "  [DRY-RUN] UPDATE: $NAME (color: $COLOR)"
      else
        if gh label edit "$NAME" \
          --repo "$REPO" \
          --color "$COLOR" \
          --description "$DESCRIPTION" 2>/dev/null; then
          echo "  [UPDATED] $NAME"
          UPDATED=$((UPDATED + 1))
        else
          echo "  [ERROR] Failed to update: $NAME" >&2
          ERRORS=$((ERRORS + 1))
        fi
      fi
    else
      echo "  [SKIPPED] $NAME (already exists, use --force to update)"
      SKIPPED=$((SKIPPED + 1))
    fi
  else
    if [ "$DRY_RUN" = true ]; then
      echo "  [DRY-RUN] CREATE: $NAME (color: $COLOR)"
      CREATED=$((CREATED + 1))
    else
      if gh label create "$NAME" \
        --repo "$REPO" \
        --color "$COLOR" \
        --description "$DESCRIPTION" 2>/dev/null; then
        echo "  [CREATED] $NAME"
        CREATED=$((CREATED + 1))
      else
        # gh label create fails if label exists (race condition), try edit
        if gh label edit "$NAME" \
          --repo "$REPO" \
          --color "$COLOR" \
          --description "$DESCRIPTION" 2>/dev/null; then
          echo "  [UPDATED] $NAME (existed, updated)"
          UPDATED=$((UPDATED + 1))
        else
          echo "  [ERROR] Failed to create: $NAME" >&2
          ERRORS=$((ERRORS + 1))
        fi
      fi
    fi
  fi
done

# --- Summary ---
echo ""
echo "============================================="
echo "[Shiki] Label setup complete"
echo "============================================="
echo "  Created:  $CREATED"
echo "  Updated:  $UPDATED"
echo "  Skipped:  $SKIPPED"
echo "  Errors:   $ERRORS"
echo "  Total:    $LABEL_COUNT"
echo ""

if [ "$DRY_RUN" = true ]; then
  echo "[Shiki] This was a dry run. No changes were made."
  echo "[Shiki] Run without --dry-run to apply changes."
fi

if [ "$ERRORS" -gt 0 ]; then
  exit 1
fi
