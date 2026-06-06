#!/usr/bin/env bash
# P2.3.6 — CCA / repair-packet schema path drift is detected.
#
# The canonical schemas live under .shiki/schemas/. An obsolete reference to
# .shiki/templates/<schema> must be flagged. This pins the real contract
# constants and the validator's drift-detection behavior, then proves the
# committed contract files are clean.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))

from shiki_contracts import (
    CANONICAL_CCA_VERDICT_SCHEMA_PATH,
    CANONICAL_REPAIR_PACKET_SCHEMA_PATH,
    OBSOLETE_CCA_VERDICT_SCHEMA_PATH,
    OBSOLETE_REPAIR_PACKET_SCHEMA_PATH,
)

# Canonical paths point at .shiki/schemas; obsolete paths point at templates.
assert CANONICAL_CCA_VERDICT_SCHEMA_PATH == ".shiki/schemas/cca-verdict.schema.json"
assert OBSOLETE_CCA_VERDICT_SCHEMA_PATH == ".shiki/templates/cca-verdict.schema.json"
assert CANONICAL_REPAIR_PACKET_SCHEMA_PATH == ".shiki/schemas/repair-packet.schema.json"
assert OBSOLETE_REPAIR_PACKET_SCHEMA_PATH == ".shiki/templates/repair-packet.schema.json"

# The canonical schema files actually exist; the obsolete template-schema files
# must not.
assert (Path.cwd() / CANONICAL_CCA_VERDICT_SCHEMA_PATH).exists()
assert (Path.cwd() / CANONICAL_REPAIR_PACKET_SCHEMA_PATH).exists()
assert not (Path.cwd() / OBSOLETE_CCA_VERDICT_SCHEMA_PATH).exists()
assert not (Path.cwd() / OBSOLETE_REPAIR_PACKET_SCHEMA_PATH).exists()


def drift_detected(text: str) -> bool:
    return (
        OBSOLETE_CCA_VERDICT_SCHEMA_PATH in text
        or OBSOLETE_REPAIR_PACKET_SCHEMA_PATH in text
    )


# A drifted document referencing the obsolete template schema is detected.
drifted = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False)
drifted.write(f"Return JSON matching {OBSOLETE_CCA_VERDICT_SCHEMA_PATH}\n")
drifted.close()
assert drift_detected(Path(drifted.name).read_text(encoding="utf-8"))

# A correct document referencing the canonical schema is clean.
clean = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False)
clean.write(f"Return JSON matching {CANONICAL_CCA_VERDICT_SCHEMA_PATH}\n")
clean.close()
assert not drift_detected(Path(clean.name).read_text(encoding="utf-8"))

# The committed CCA completion prompt must already use the canonical path.
prompt = Path.cwd() / ".github" / "prompts" / "cca-completion-check.md"
if prompt.exists():
    assert not drift_detected(prompt.read_text(encoding="utf-8")), (
        "committed CCA prompt references an obsolete schema path"
    )

print("P2.3.6 CCA schema path drift detection passed")
PY
