# Branch Protection Smoke Test

This document is a small, low-risk change used to verify that Shiki branch protection, required checks, CCA, and MergeGate run on a normal pull request after the initial bootstrap has landed on `main`.

Expected gates:

- Validate Shiki mirror
- CCA verdict
- MergeGate policy check

This file can remain as durable evidence for the first post-bootstrap gate test.
