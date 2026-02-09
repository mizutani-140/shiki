#!/usr/bin/env bash
# start_cli_session.sh — Shiki（式） CLIモードセッション起動スクリプト
#
# 使用方法:
#   ./scripts/start_cli_session.sh [--resume]
#
# 概要:
#   1. .shiki/config.yaml から cli.display 設定を読み取る
#   2. 表示モードに応じてセッション環境を構築する
#   3. 復旧ファイルがある場合はリジューム提案を行う
#   4. Claude Code を Agent Teams モードで起動する

set -euo pipefail

# --- 定数 ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${PROJECT_ROOT}/.shiki/config.yaml"
STATE_DIR="${PROJECT_ROOT}/.shiki/state"
TMUX_SESSION_NAME="shiki"
RECOVER_FLAG="${1:-}"

# --- 色定義 ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# --- ユーティリティ関数 ---
log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "${CYAN}[STEP]${NC} $*"; }

# --- 設定読み取り ---
# YAMLパーサが無い環境でも動作するよう、grep/sed で最低限の値を取得する
read_config_value() {
    local key="$1"
    local default="$2"
    if [[ -f "${CONFIG_FILE}" ]]; then
        local value
        value=$(grep -E "^\s*${key}:" "${CONFIG_FILE}" 2>/dev/null | head -1 | sed 's/.*:\s*//' | sed 's/\s*#.*//' | tr -d '[:space:]')
        if [[ -n "${value}" ]]; then
            echo "${value}"
            return
        fi
    fi
    echo "${default}"
}

get_display_mode() {
    read_config_value "display" "tmux"
}

get_delegate_mode() {
    read_config_value "delegate_mode" "true"
}

get_self_claim() {
    read_config_value "self_claim" "true"
}

get_plan_mode_required() {
    read_config_value "plan_mode_required" "true"
}

# --- 復旧チェック ---
check_recovery() {
    local latest_session=""
    if [[ -d "${STATE_DIR}" ]]; then
        latest_session=$(find "${STATE_DIR}" -name 'session-*.json' -type f 2>/dev/null | sort -r | head -1)
    fi

    if [[ -n "${latest_session}" ]]; then
        echo "${latest_session}"
        return 0
    fi
    return 1
}

offer_resume() {
    local session_file="$1"
    local session_id
    local timestamp
    local theta_phase

    # JSON から基本情報を抽出（jq がある場合）
    if command -v jq &>/dev/null; then
        session_id=$(jq -r '.session_id // "unknown"' "${session_file}" 2>/dev/null)
        timestamp=$(jq -r '.timestamp // "unknown"' "${session_file}" 2>/dev/null)
        theta_phase=$(jq -r '.theta_phase // "unknown"' "${session_file}" 2>/dev/null)
    else
        session_id=$(grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' "${session_file}" 2>/dev/null | head -1 | sed 's/.*"session_id"[[:space:]]*:[[:space:]]*"//' | sed 's/"//')
        timestamp=$(grep -o '"timestamp"[[:space:]]*:[[:space:]]*"[^"]*"' "${session_file}" 2>/dev/null | head -1 | sed 's/.*"timestamp"[[:space:]]*:[[:space:]]*"//' | sed 's/"//')
        theta_phase=$(grep -o '"theta_phase"[[:space:]]*:[[:space:]]*"[^"]*"' "${session_file}" 2>/dev/null | head -1 | sed 's/.*"theta_phase"[[:space:]]*:[[:space:]]*"//' | sed 's/"//')
    fi

    echo ""
    echo -e "${YELLOW}============================================${NC}"
    echo -e "${YELLOW}  前回のセッションが見つかりました${NC}"
    echo -e "${YELLOW}============================================${NC}"
    echo -e "  セッションID: ${CYAN}${session_id:-unknown}${NC}"
    echo -e "  タイムスタンプ: ${CYAN}${timestamp:-unknown}${NC}"
    echo -e "  θフェーズ: ${CYAN}${theta_phase:-unknown}${NC}"
    echo -e "  ファイル: ${CYAN}${session_file}${NC}"
    echo ""

    if [[ "${RECOVER_FLAG}" == "--resume" ]]; then
        log_info "自動リジュームモード（--resume フラグ検出）"
        return 0
    fi

    read -p "前回のセッションから再開しますか？ [Y/n]: " answer
    case "${answer}" in
        [nN]|[nN][oO])
            log_info "新規セッションを開始します"
            return 1
            ;;
        *)
            log_info "前回のセッションから再開します"
            return 0
            ;;
    esac
}

# --- tmux セットアップ ---
setup_tmux() {
    log_step "tmux セッションをセットアップ中..."

    if ! command -v tmux &>/dev/null; then
        log_error "tmux がインストールされていません"
        log_info "インストール: brew install tmux (macOS) / apt install tmux (Linux)"
        exit 1
    fi

    # 既存セッションの確認
    if tmux has-session -t "${TMUX_SESSION_NAME}" 2>/dev/null; then
        log_warn "既存の '${TMUX_SESSION_NAME}' セッションが見つかりました"
        read -p "既存セッションに接続しますか？ [Y/n]: " answer
        case "${answer}" in
            [nN]|[nN][oO])
                log_info "既存セッションを終了して新規作成します"
                tmux kill-session -t "${TMUX_SESSION_NAME}" 2>/dev/null || true
                ;;
            *)
                log_info "既存セッションに接続します"
                tmux attach-session -t "${TMUX_SESSION_NAME}"
                exit 0
                ;;
        esac
    fi

    # 新規セッション作成: メインペイン（Leader/Coordinator）
    tmux new-session -d -s "${TMUX_SESSION_NAME}" -n "leader" -x 220 -y 50

    # ペイン分割: 左=Leader, 右上=Member1, 右下=Member2
    tmux split-window -h -t "${TMUX_SESSION_NAME}:leader"
    tmux split-window -v -t "${TMUX_SESSION_NAME}:leader.1"

    # ペインタイトル設定
    tmux select-pane -t "${TMUX_SESSION_NAME}:leader.0" -T "Leader (Coordinator)"
    tmux select-pane -t "${TMUX_SESSION_NAME}:leader.1" -T "Member 1 (Executor)"
    tmux select-pane -t "${TMUX_SESSION_NAME}:leader.2" -T "Member 2 (Executor/Monitor)"

    # ステータスバー設定
    tmux set-option -t "${TMUX_SESSION_NAME}" status-left "#[fg=cyan][Shiki]#[default] "
    tmux set-option -t "${TMUX_SESSION_NAME}" status-right "#[fg=yellow]θ: #{?#{==:#{pane_title},Leader},COORD,MEMBER}#[default] | %H:%M"
    tmux set-option -t "${TMUX_SESSION_NAME}" pane-border-status top
    tmux set-option -t "${TMUX_SESSION_NAME}" pane-border-format " #{pane_title} "

    # 監視用ウィンドウを追加
    tmux new-window -t "${TMUX_SESSION_NAME}" -n "monitor"
    tmux send-keys -t "${TMUX_SESSION_NAME}:monitor" "watch -n 5 'cat ${STATE_DIR}/session-*.json 2>/dev/null | python3 -m json.tool 2>/dev/null || echo \"No active session\"'" C-m

    # Leader ペインにフォーカスを戻す
    tmux select-window -t "${TMUX_SESSION_NAME}:leader"
    tmux select-pane -t "${TMUX_SESSION_NAME}:leader.0"

    log_info "tmux セッション '${TMUX_SESSION_NAME}' を作成しました"
    log_info "レイアウト:"
    echo "  ┌─────────────────┬─────────────────┐"
    echo "  │                 │   Member 1      │"
    echo "  │   Leader        │   (Executor)    │"
    echo "  │   (Coordinator) ├─────────────────┤"
    echo "  │                 │   Member 2      │"
    echo "  │                 │   (Exec/Monitor) │"
    echo "  └─────────────────┴─────────────────┘"
    echo ""
    echo "  ウィンドウ 2: [monitor] — セッション状態の監視"
    echo ""
}

# --- iTerm2 セットアップ ---
setup_iterm2() {
    log_step "iTerm2 セットアップ..."

    if [[ "$(uname)" != "Darwin" ]]; then
        log_error "iTerm2 は macOS でのみ使用できます"
        exit 1
    fi

    echo ""
    echo -e "${BLUE}============================================${NC}"
    echo -e "${BLUE}  iTerm2 手動セットアップ手順${NC}"
    echo -e "${BLUE}============================================${NC}"
    echo ""
    echo "以下の AppleScript を iTerm2 で実行するか、手動でペインを作成してください："
    echo ""
    echo -e "${CYAN}--- AppleScript ここから ---${NC}"
    cat << 'APPLESCRIPT'
tell application "iTerm2"
    create window with default profile
    tell current session of current window
        set name to "Leader (Coordinator)"
        write text "export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1"
    end tell
    tell current window
        -- 右ペイン作成（Member 1）
        set member1Tab to (create tab with default profile)
        tell current session of member1Tab
            set name to "Member 1 (Executor)"
            write text "export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1"
        end tell
        -- 右下ペイン作成（Member 2）
        set member2Tab to (create tab with default profile)
        tell current session of member2Tab
            set name to "Member 2 (Executor/Monitor)"
            write text "export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1"
        end tell
    end tell
end tell
APPLESCRIPT
    echo -e "${CYAN}--- AppleScript ここまで ---${NC}"
    echo ""
    echo "または、iTerm2 で手動でペインを分割してください："
    echo "  1. Cmd+D で垂直分割（Leader | Member1）"
    echo "  2. 右ペインで Cmd+Shift+D で水平分割（Member1 / Member2）"
    echo "  3. 各ペインで 'export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1' を実行"
    echo ""
}

# --- in-process セットアップ ---
setup_in_process() {
    log_step "in-process モードで起動します..."
    log_info "単一ターミナルで Claude Code を起動します（ペイン分割なし）"
    echo ""
}

# --- Claude Code 起動 ---
launch_claude() {
    local resume_prompt=""

    # shiki start からの初期プロンプト（SHIKI_INIT_PROMPT）
    if [[ -n "${SHIKI_INIT_PROMPT:-}" ]]; then
        resume_prompt="${SHIKI_INIT_PROMPT}"
    fi

    # 復旧プロンプトの準備（復旧が優先）
    if [[ -n "${RECOVERY_SESSION_FILE:-}" ]]; then
        log_step "復旧プロンプトを生成中..."
        if [[ -f "${SCRIPT_DIR}/recover_session.py" ]]; then
            resume_prompt=$(python3 "${SCRIPT_DIR}/recover_session.py" "${RECOVERY_SESSION_FILE}" 2>/dev/null || echo "")
        fi
        if [[ -z "${resume_prompt}" ]]; then
            resume_prompt="前回のセッションから再開してください。セッション状態ファイル: ${RECOVERY_SESSION_FILE} を読み込み、未完了タスクを確認して作業を再開してください。"
        fi
    fi

    # 環境変数設定
    export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1

    local display_mode
    display_mode=$(get_display_mode)

    log_info "Agent Teams モードを有効化しました"
    log_info "表示モード: ${display_mode}"
    log_info "Delegate Mode: $(get_delegate_mode)"
    log_info "Self-Claim: $(get_self_claim)"
    log_info "Plan Mode Required: $(get_plan_mode_required)"

    # --- Dual Engine 認証チェック ---
    echo ""
    log_step "Dual Engine 認証チェック..."

    # Claude CLI
    if command -v claude &>/dev/null; then
        log_info "Claude CLI: OK"
    else
        log_warn "Claude CLI: not found"
    fi

    # Codex CLI + 認証状態
    if command -v codex &>/dev/null; then
        if codex login status &>/dev/null; then
            log_info "Codex CLI: OK (authenticated)"
        elif [[ -n "${OPENAI_API_KEY:-}" ]]; then
            log_info "Codex CLI: OK (API key)"
        else
            log_warn "Codex CLI: installed but not authenticated"
            log_warn "  Pro plan: codex login"
            log_warn "  API key:  export OPENAI_API_KEY=\"sk-...\""
        fi
    else
        log_warn "Codex CLI: not installed (Dual Engine disabled)"
        log_warn "  Install: npm i -g @openai/codex"
    fi
    echo ""

    # Claude Code 起動
    if [[ "${display_mode}" == "tmux" ]]; then
        if [[ -n "${resume_prompt}" ]]; then
            log_info "Leader ペインで Claude Code を起動（復旧モード）..."
            tmux send-keys -t "${TMUX_SESSION_NAME}:leader.0" "cd '${PROJECT_ROOT}' && export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 && claude --prompt '${resume_prompt}'" C-m
        else
            log_info "Leader ペインで Claude Code を起動..."
            tmux send-keys -t "${TMUX_SESSION_NAME}:leader.0" "cd '${PROJECT_ROOT}' && export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 && claude" C-m
        fi
        echo ""
        log_info "tmux セッションに接続します..."
        tmux attach-session -t "${TMUX_SESSION_NAME}"
    elif [[ "${display_mode}" == "iterm2" ]]; then
        log_info "iTerm2 の各ペインで以下を実行してください："
        echo "  cd '${PROJECT_ROOT}'"
        echo "  export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1"
        echo "  claude"
        if [[ -n "${resume_prompt}" ]]; then
            echo ""
            echo "復旧プロンプト："
            echo "  ${resume_prompt}"
        fi
    else
        # in-process
        cd "${PROJECT_ROOT}"
        if [[ -n "${resume_prompt}" ]]; then
            log_info "Claude Code を復旧モードで起動..."
            exec claude --prompt "${resume_prompt}"
        else
            log_info "Claude Code を起動..."
            exec claude
        fi
    fi
}

# --- メイン処理 ---
main() {
    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║     Shiki（式） — CLI Session Start      ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
    echo ""

    # プロジェクトルートの確認
    if [[ ! -f "${CONFIG_FILE}" ]]; then
        log_error ".shiki/config.yaml が見つかりません"
        log_info "先に ./scripts/init_shiki.sh を実行してください"
        exit 1
    fi

    log_info "プロジェクト: ${PROJECT_ROOT}"

    # 復旧チェック
    RECOVERY_SESSION_FILE=""
    if check_recovery; then
        local session_file
        session_file=$(check_recovery && find "${STATE_DIR}" -name 'session-*.json' -type f 2>/dev/null | sort -r | head -1)
        if offer_resume "${session_file}"; then
            RECOVERY_SESSION_FILE="${session_file}"
            log_info "復旧セッション: ${RECOVERY_SESSION_FILE}"
        fi
    fi

    # 表示モードに応じたセットアップ
    local display_mode
    display_mode=$(get_display_mode)

    case "${display_mode}" in
        tmux)
            setup_tmux
            ;;
        iterm2)
            setup_iterm2
            ;;
        in-process)
            setup_in_process
            ;;
        *)
            log_warn "不明な表示モード '${display_mode}'。in-process で起動します"
            setup_in_process
            ;;
    esac

    # Claude Code 起動
    launch_claude
}

main "$@"
