# Issue Tracker

Shiki uses GitHub as the primary issue tracker for Target Repositories.

## Source Of Truth

- GitHub Issue: Goal intake, discussion, scope, lifecycle, and Guardian decisions.
- GitHub Pull Request: implementation evidence, review, checks, and merge decision.
- GitHub Actions: execution log and verification evidence.
- `.shiki/` mirror: portable recovery and audit state derived from GitHub and agent execution.

## Rules

- Every substantial Goal should have a GitHub Issue.
- Every implementation should land through a Pull Request.
- PRs must link their Goal or task.
- Review findings should be left as PR comments or check output.
- If `.shiki/` and GitHub disagree, repair the mirror rather than treating local files as more current by default.
