# Node 24 Workflow Compatibility

This inventory is the source of truth for Shiki workflow runtime compatibility
decisions. Official GitHub JavaScript actions should run on Node 24-compatible
major versions unless a dedicated Guardian-approved migration records a narrower
exception.

`ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION` is forbidden. Shiki workflows that run
JavaScript actions set `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true` so CI exercises
the same compatibility surface that production GitHub Actions runners will use.

## Action Inventory

| Workflow | Job | Step / action | Current version | Node 20 warning status | Node 24-compatible candidate | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| `shiki-cca-completion.yml` | CCA verdict | `actions/checkout` | `actions/checkout@v6` | resolved official action | `actions/checkout@v6` | upgraded in T-0041 |
| `shiki-cca-completion.yml` | CCA verdict | `anthropics/claude-code-action` | `anthropics/claude-code-action@v1` | deferred third-party action | none verified in this task | exact defer: workflow/action/version only |
| `shiki-cca-completion.yml` | CCA verdict | `actions/upload-artifact` | `actions/upload-artifact@v7` | resolved official action | `actions/upload-artifact@v7` | upgraded in T-0041 |
| `shiki-cca-completion.yml` | MergeGate policy check | `actions/checkout` | `actions/checkout@v6` | resolved official action | `actions/checkout@v6` | upgraded in T-0041 |
| `shiki-cca-completion.yml` | MergeGate policy check | `actions/download-artifact` | `actions/download-artifact@v8` | resolved official action | `actions/download-artifact@v8` | upgraded in T-0041 |
| `shiki-claude-review.yml` | Claude review | `actions/checkout` | `actions/checkout@v6` | resolved official action | `actions/checkout@v6` | upgraded in T-0041 |
| `shiki-claude-review.yml` | Claude review | `anthropics/claude-code-action` | `anthropics/claude-code-action@v1` | deferred third-party action | none verified in this task | exact defer: workflow/action/version only |
| `shiki-orchestrator.yml` | Shiki orchestrator run | `actions/checkout` | `actions/checkout@v5` | resolved official action | `actions/checkout@v5` or `actions/checkout@v6` | already compatible |
| `shiki-orchestrator.yml` | Commit Shiki evidence PR | `actions/checkout` | `actions/checkout@v5` | resolved official action | `actions/checkout@v5` or `actions/checkout@v6` | already compatible |
| `shiki-validate.yml` | Validate Shiki mirror | `actions/checkout` | `actions/checkout@v5` | resolved official action | `actions/checkout@v5` or `actions/checkout@v6` | already compatible |
| `shiki-mergegate.yml` | MergeGate metadata check | `actions/checkout` | `actions/checkout@v5` | resolved official action | `actions/checkout@v5` or `actions/checkout@v6` | already compatible |

## Anthropic Action Constraint

`anthropics/claude-code-action@v1` remains an exact deferred third-party action
for the CCA and Claude review workflows. Those workflow files participate in
Anthropic action OIDC token exchange, and pull-request validation can fail when
the workflow file content does not match the default branch version that GitHub
uses for OIDC policy evaluation.

That constraint does not allow broad defers. The validator accepts only these
exact entries:

- `shiki-cca-completion.yml` with `anthropics/claude-code-action@v1`
- `shiki-claude-review.yml` with `anthropics/claude-code-action@v1`

If either workflow moves to a different Anthropic action version, validation
must fail until the new version is verified and this inventory is updated in a
dedicated Guardian-approved task.

## Post-Merge Verification

After the T-0041 PR merges to `main`, run the affected workflows from `main`:

```bash
gh workflow run "Shiki CCA Completion" --repo mizutani-140/shiki --ref main -f pr_number=<verification-pr>
gh workflow run "Shiki Claude Review" --repo mizutani-140/shiki --ref main
gh workflow run "Shiki Validate" --repo mizutani-140/shiki --ref main
```

Then confirm:

- official actions updated in T-0041 no longer emit Node 20 deprecation warnings;
- any remaining Node runtime warning is tied only to the documented
  `anthropics/claude-code-action@v1` exact exceptions;
- CCA, Claude review, Validate, MergeGate metadata, and MergeGate policy checks
  still pass on the current head;
- the reconciliation ledger records the workflow run links and any remaining
  exact third-party defer.
