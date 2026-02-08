#!/usr/bin/env python3
"""Claude Code Hook: PreToolUse (Bash)

- Blocks obviously dangerous commands
- Prevents accidental secret leakage to logs (OPENAI_API_KEY / ANTHROPIC_API_KEY etc.)
- Prevents worktree deletion, tmux kill-server, and budget-exceeding patterns

Expected input (stdin): JSON
We output JSON when we want to deny or warn.
"""

import json
import re
import sys
from typing import Any, Dict

DANGEROUS_PATTERNS = [
    # Secret exfiltration
    r"\b(printenv|env)\b.*\b(KEY|TOKEN|SECRET)\b",
    r"\becho\b\s+\$?\{?(OPENAI_API_KEY|ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN|GITHUB_TOKEN)\}?",
    r"\bcat\b\s+.*(\.env|id_rsa|authorized_keys)\b",
    # Codex credential protection (~/.codex/auth.json, auth_token.json)
    r"\bcat\b\s+.*\.codex/(auth\.json|auth_token\.json)\b",
    r"\bcp\b\s+.*\.codex/(auth\.json|auth_token\.json)\b",
    r"\bcurl\b.*\.codex/(auth\.json|auth_token\.json)\b",
    # Destructive
    r"\brm\b\s+-rf\s+/",
    r"\bmkfs\.",
    r"\bdd\b\s+if=",
    # Git footguns
    r"\bgit\b\s+push\b.*--force\b.*\b(main|master)\b",
    r"\bgit\b\s+reset\b\s+--hard\b",
    r"\bgit\b\s+clean\b\s+-fd\b",
    # Worktree protection
    r"\bgit\b\s+worktree\b\s+remove\b.*--force\b",
    r"\brm\b\s+-rf\b.*worktrees?\b",
    # tmux protection
    r"\btmux\b\s+kill-server\b",
    # Budget/resource abuse
    r"\bwhile\b\s+true\b.*\bcurl\b",
    r"\bfork\b\s+bomb\b",
]

def main() -> int:
    try:
        data: Dict[str, Any] = json.load(sys.stdin)
    except Exception:
        # If we cannot parse input, don't block.
        return 0

    tool_input = data.get("tool_input") or {}
    cmd = tool_input.get("command") or ""
    if not isinstance(cmd, str):
        return 0

    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, cmd, flags=re.IGNORECASE):
            out = {
                "decision": "deny",
                "reason": f"Blocked dangerous command pattern: {pattern}",
            }
            print(json.dumps(out))
            # non-zero exit tells Claude Code hook to deny
            return 2

    return 0

if __name__ == "__main__":
    sys.exit(main())
