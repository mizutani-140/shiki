# Shiki CLI Architecture

`scripts/shiki.py` is the executable CLI shim. It keeps the shebang, imports the
parser and `main()` from `shiki_cli.py`, and exposes a small set of transitional
compatibility names for existing tests and installed target repositories.

The implementation remains dependency-free and uses only the Python standard
library. Modules must not perform git, GitHub, filesystem mutation, network, or
command execution work at import time.

## Module Boundaries

| Module | Responsibility |
| --- | --- |
| `scripts/shiki_cli.py` | `argparse` construction, subcommand registration, and dispatch to command functions. |
| `scripts/shiki_process.py` | Process execution, console output, common paths, JSON helpers, and dependency-free utility functions. |
| `scripts/shiki_git.py` | Local git repository detection, remote adoption checks, manifest staging commits, branch pushes, and worktree branch probes. |
| `scripts/shiki_github.py` | GitHub CLI/API interactions for repository creation, secrets, branch protection, issue/PR evidence, and GitHub origin parsing. |
| `scripts/shiki_provider.py` | Dependency-free GitHub-compatible provider config, canonical remote URL generation, remote matching, repo API paths, and `GH_HOST` environment mapping. |
| `scripts/shiki_config.py` | Dependency-free `.shiki/config.yaml` subset parsing and branch-protection review-count derivation. |
| `scripts/shiki_installer.py` | Template path list, target installation, manifest commit exclusions, and local/global command installation. |
| `scripts/shiki_migrations.py` | Dependency-free `.shiki` migration registry, state loading, status/plan/apply behavior, dry-run / execute gate, and migration CLI command implementation. |
| `scripts/shiki_bootstrap.py` | `init`, `bootstrap-platform`, `bootstrap-github`, `preflight`, and `start` orchestration, including dry-run / execute gating. |
| `scripts/shiki_tasks.py` | Goal, task, DAG, ledger, lock, worktree-record, repair-packet, and handoff lifecycle helpers. |
| `scripts/shiki_memory.py` | Memory Loop: capture/promotion state machine with redaction, the ledger-derived scorecard, and deterministic Consult selection of active distilled rules for handoff injection (proposal 0001 v2 §3.1–3.7). |
| `scripts/shiki_loop.py` | Autonomous post-freeze goal loop: pure decision engine (dispatch / create_pr / rerun_cca / merge / repair / unblock / complete) plus effectors and the `loop step`/`loop run` commands. |
| `scripts/shiki_runtime.py` | Current daemon, runtime-agnostic runner dispatch (`runner claude` / `runner codex`), smoke, and entrypoint status helpers. |
| `scripts/shiki_runtime_adapters.py` | Runner adapter boundary: per-runtime tool/auth probes and headless execution commands bound to registry runtime names (ADR 0008). |

Existing support modules remain canonical for their domains:

- `scripts/shiki_state.py`
- `scripts/shiki_manifest.py`
- `scripts/shiki_migrations.py`
- `scripts/shiki_provider.py`
- `scripts/shiki_locks.py`
- `scripts/shiki_workflows.py`
- `scripts/shiki_jsonschema.py`
- `scripts/shiki_contracts.py`

## Compatibility

Temporary compatibility exports from `scripts/shiki.py` are allowed only for
existing tests or installed target repositories. New code should import from the
canonical `shiki_*` module directly.

`scripts/test_shiki_module_boundaries.sh` and `scripts/validate_shiki.py` enforce
that the shim remains small, new modules import successfully, template staging
includes required modules, and CLI help still exposes the existing command set.
