#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))

import validate_shiki
from shiki_jsonschema import JsonSchemaError, UnsupportedJsonSchemaError, validate_json_schema
from shiki_workflows import load_workflow_model
from validate_shiki import ValidationError


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def expect_pass(name: str, callback) -> None:
    try:
        callback()
    except Exception as error:
        raise SystemExit(f"{name}: expected pass, got {error}") from error


def expect_fail(name: str, callback, expected: str) -> None:
    try:
        callback()
    except Exception as error:
        if expected not in str(error):
            raise SystemExit(f"{name}: expected {expected!r}, got {error}") from error
        return
    raise SystemExit(f"{name}: expected failure")


def with_temp_root(callback) -> None:
    original_root = validate_shiki.ROOT
    with tempfile.TemporaryDirectory(prefix="shiki-validator-hardening-") as tmp:
        root = Path(tmp)
        write(
            root / ".shiki" / "config.yaml",
            "mergegate:\n"
            "  required_checks:\n"
            "    - Validate Shiki mirror\n",
        )
        validate_shiki.ROOT = root
        try:
            callback(root)
        finally:
            validate_shiki.ROOT = original_root


VALID_WORKFLOW = """\
name: Fixture
on:
  pull_request:
  workflow_dispatch:
permissions:
  contents: read
jobs:
  validate:
    name: Validate Shiki mirror
    runs-on: ubuntu-latest
    steps:
      - name: Validate
        run: python3 scripts/validate_shiki.py
"""


def model_from(root: Path, text: str):
    return load_workflow_model(write(root / ".github" / "workflows" / "fixture.yml", text))


def model_at(path: Path, text: str):
    return load_workflow_model(write(path, text))


def node24_inventory_doc(*, include_deferred: bool = True) -> str:
    rows = [
        "# Node 24 Workflow Compatibility",
        "",
        "| Workflow | Job | Step / action | Current version | Node 20 warning status | Node 24-compatible candidate | Decision |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    if include_deferred:
        rows.extend(
            [
                "| shiki-cca-completion.yml | CCA verdict | actions/checkout | actions/checkout@v4 | deferred official action | actions/checkout@v6 | exact two-phase defer |",
                "| shiki-cca-completion.yml | CCA verdict | anthropics/claude-code-action | anthropics/claude-code-action@v1 | deferred third-party action | none verified | exact defer |",
                "| shiki-cca-completion.yml | CCA verdict | actions/upload-artifact | actions/upload-artifact@v4 | deferred official action | actions/upload-artifact@v7 | exact two-phase defer |",
                "| shiki-cca-completion.yml | MergeGate policy check | actions/checkout | actions/checkout@v4 | deferred official action | actions/checkout@v6 | exact two-phase defer |",
                "| shiki-cca-completion.yml | MergeGate policy check | actions/download-artifact | actions/download-artifact@v4 | deferred official action | actions/download-artifact@v8 | exact two-phase defer |",
                "| shiki-claude-review.yml | Claude review | actions/checkout | actions/checkout@v4 | deferred official action | actions/checkout@v6 | exact two-phase defer |",
                "| shiki-claude-review.yml | Claude review | anthropics/claude-code-action | anthropics/claude-code-action@v1 | deferred third-party action | none verified | exact defer |",
            ]
        )
    return "\n".join(rows) + "\n"


with_temp_root(
    lambda root: expect_pass(
        "valid workflow required check",
        lambda: validate_shiki.validate_required_check_names({root / ".github/workflows/fixture.yml": model_from(root, VALID_WORKFLOW)}),
    )
)

with_temp_root(
    lambda root: expect_fail(
        "missing job name",
        lambda: validate_shiki.validate_required_check_names(
            {
                root / ".github/workflows/fixture.yml": model_from(
                    root,
                    VALID_WORKFLOW.replace("name: Validate Shiki mirror", "name: Validate mirror drift"),
                )
            }
        ),
        "has no matching workflow job display name",
    )
)

with_temp_root(
    lambda root: expect_fail(
        "required check comment does not satisfy",
        lambda: validate_shiki.validate_required_check_names(
            {
                root / ".github/workflows/fixture.yml": model_from(
                    root,
                    VALID_WORKFLOW.replace("name: Validate Shiki mirror", "# name: Validate Shiki mirror\n    name: Other"),
                )
            }
        ),
        "has no matching workflow job display name",
    )
)

with_temp_root(
    lambda root: expect_fail(
        "required check unrelated string does not satisfy",
        lambda: validate_shiki.validate_required_check_names(
            {
                root / ".github/workflows/fixture.yml": model_from(
                    root,
                    VALID_WORKFLOW.replace(
                        "run: python3 scripts/validate_shiki.py",
                        "run: echo 'Validate Shiki mirror'",
                    ).replace("name: Validate Shiki mirror", "name: Other"),
                )
            }
        ),
        "has no matching workflow job display name",
    )
)

with_temp_root(
    lambda root: expect_fail(
        "missing trigger",
        lambda: validate_shiki.validate_workflow_contracts(),
        "required workflow file is missing",
    )
)

with_temp_root(
    lambda root: expect_fail(
        "job id alone does not satisfy display name",
        lambda: validate_shiki.validate_required_check_names(
            {
                root / ".github/workflows/fixture.yml": model_from(
                    root,
                    VALID_WORKFLOW.replace(
                        "  validate:\n    name: Validate Shiki mirror",
                        "  Validate Shiki mirror:\n    name: Different display",
                    ),
                )
            }
        ),
        "has no matching workflow job display name",
    )
)

with_temp_root(
    lambda root: expect_fail(
        "duplicate job display name",
        lambda: validate_shiki.validate_required_check_names(
            {
                root / ".github/workflows/fixture.yml": model_from(
                    root,
                    VALID_WORKFLOW
                    + "  duplicate:\n"
                    + "    name: Validate Shiki mirror\n"
                    + "    runs-on: ubuntu-latest\n"
                    + "    steps:\n"
                    + "      - name: Echo\n"
                    + "        run: echo ok\n",
                )
            }
        ),
        "duplicate workflow job display name",
    )
)

with_temp_root(
    lambda root: expect_fail(
        "config check without job fails",
        lambda: (
            write(
                root / ".shiki" / "config.yaml",
                "mergegate:\n  required_checks:\n    - Obsolete required check\n",
            ),
            validate_shiki.validate_required_check_names({root / ".github/workflows/fixture.yml": model_from(root, VALID_WORKFLOW)}),
        ),
        "Obsolete required check",
    )
)

expect_pass("repo workflow contracts", validate_shiki.validate_workflow_contracts)

schema = {
    "type": "object",
    "required": ["status", "items"],
    "properties": {
        "status": {"type": "string", "enum": ["ok"]},
        "items": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^x"}},
    },
    "additionalProperties": False,
}
expect_pass("valid schema fixture", lambda: validate_json_schema({"status": "ok", "items": ["x1"]}, schema))
expect_fail("missing required field", lambda: validate_json_schema({"items": ["x1"]}, schema), "missing required")
expect_fail("wrong type", lambda: validate_json_schema({"status": "ok", "items": "x1"}, schema), "expected type")
expect_fail(
    "additional property",
    lambda: validate_json_schema({"status": "ok", "items": ["x1"], "extra": True}, schema),
    "unexpected additional properties",
)
expect_fail("unsupported ref", lambda: validate_json_schema({}, {"$ref": "#/$defs/x"}), "$ref")
expect_fail("unsupported oneOf", lambda: validate_json_schema({}, {"oneOf": [{"type": "object"}]}), "oneOf")
expect_fail("unsupported format", lambda: validate_json_schema("x", {"type": "string", "format": "uri"}), "format")

expect_pass("repo JSON Schema contracts", validate_shiki.validate_json_schema_contracts)

with tempfile.TemporaryDirectory(prefix="shiki-node24-policy-") as tmp:
    path = Path(tmp) / ".github" / "workflows" / "node.yml"
    model = load_workflow_model(
        write(
            path,
            """\
name: Node Policy
on:
  pull_request:
env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true
permissions:
  contents: read
jobs:
  validate:
    name: Validate Shiki mirror
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v5
      - name: Upload
        uses: actions/upload-artifact@v7
      - name: Download
        uses: actions/download-artifact@v8
""",
        )
    )
    expect_pass("node24 force accepted", lambda: validate_shiki.validate_node24_workflow_policy({path: model}))
    unsafe = path.read_text(encoding="utf-8").replace(
        "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true",
        "ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION: true",
    )
    unsafe_model = load_workflow_model(write(path, unsafe))
    expect_fail(
        "unsafe node env fails",
        lambda: validate_shiki.validate_node24_workflow_policy({path: unsafe_model}),
        "ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION",
    )
    deprecated = unsafe.replace("ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION: true", "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true").replace(
        "actions/checkout@v5",
        "actions/checkout@v4",
    )
    deprecated_model = load_workflow_model(write(path, deprecated))
    expect_fail(
        "deprecated official action fails deterministically",
        lambda: validate_shiki.validate_node24_workflow_policy({path: deprecated_model}),
        "Node 24-compatible official action",
    )
    wildcard = deprecated.replace("actions/checkout@v4", "actions/checkout@v*")
    wildcard_model = load_workflow_model(write(path, wildcard))
    expect_fail(
        "wildcard action version fails deterministically",
        lambda: validate_shiki.validate_node24_workflow_policy({path: wildcard_model}),
        "wildcard",
    )

with_temp_root(
    lambda root: (
        write(root / "docs" / "agents" / "node24-workflow-compatibility.md", node24_inventory_doc()),
        expect_pass(
            "exact deferred Claude action passes with docs",
            lambda: validate_shiki.validate_node24_workflow_policy(
                {
                    root
                    / ".github/workflows/shiki-cca-completion.yml": model_at(
                        root / ".github/workflows/shiki-cca-completion.yml",
                        """\
name: Shiki CCA Completion
on:
  pull_request:
env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true
permissions:
  contents: read
jobs:
  cca:
    name: CCA verdict
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Run CCA
        uses: anthropics/claude-code-action@v1
      - name: Upload
        uses: actions/upload-artifact@v4
  mergegate:
    name: MergeGate policy check
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Download
        uses: actions/download-artifact@v4
""",
                    )
                }
            ),
        ),
    )
)

with_temp_root(
    lambda root: (
        write(root / "docs" / "agents" / "node24-workflow-compatibility.md", node24_inventory_doc(include_deferred=False)),
        expect_fail(
            "deferred Claude action requires inventory docs",
            lambda: validate_shiki.validate_node24_workflow_policy(
                {
                    root
                    / ".github/workflows/shiki-cca-completion.yml": model_at(
                        root / ".github/workflows/shiki-cca-completion.yml",
                        """\
name: Shiki CCA Completion
on:
  pull_request:
env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true
permissions:
  contents: read
jobs:
  cca:
    name: CCA verdict
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Run CCA
        uses: anthropics/claude-code-action@v1
      - name: Upload
        uses: actions/upload-artifact@v4
  mergegate:
    name: MergeGate policy check
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Download
        uses: actions/download-artifact@v4
""",
                    )
                }
            ),
            "missing deferred action inventory",
        ),
    )
)

with_temp_root(
    lambda root: (
        write(root / "docs" / "agents" / "node24-workflow-compatibility.md", node24_inventory_doc()),
        expect_fail(
            "changed Claude action version is not implicitly deferred",
            lambda: validate_shiki.validate_node24_workflow_policy(
                {
                    root
                    / ".github/workflows/shiki-cca-completion.yml": model_at(
                        root / ".github/workflows/shiki-cca-completion.yml",
                        """\
name: Shiki CCA Completion
on:
  pull_request:
env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true
permissions:
  contents: read
jobs:
  cca:
    name: CCA verdict
    runs-on: ubuntu-latest
    steps:
      - name: Run CCA
        uses: anthropics/claude-code-action@v2
""",
                    )
                }
            ),
            "explicit Node 24 deferred action exception",
        ),
    )
)

print("Shiki validator hardening tests passed")
PY
