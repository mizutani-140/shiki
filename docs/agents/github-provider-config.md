# GitHub Provider Configuration

Shiki remains GitHub-first. Provider configuration makes GitHub host and remote
protocol assumptions explicit; it does not add GitLab, Bitbucket, or a provider
plugin system.

## Defaults

The default target provider is GitHub.com over HTTPS:

```json
{
  "provider": "github",
  "host": "github.com",
  "remote_protocol": "https",
  "web_base_url": "https://github.com",
  "api_base_url": "https://api.github.com"
}
```

For `OWNER/REPO`, the canonical default remote is:

```text
https://github.com/OWNER/REPO.git
```

## SSH Remotes

Use `--remote-protocol ssh` when the target repository should use SSH origin
URLs:

```bash
shiki init /path/to/repo --repo OWNER/REPO --remote-protocol ssh
```

The canonical SSH remote is:

```text
git@github.com:OWNER/REPO.git
```

Remote adoption checks compare host and repo identity. HTTPS and SSH remotes
for the same configured host and repo are treated as the same target, while
different hosts or different repo slugs remain mismatches unless
`--adopt-existing-repo` is passed.

## GitHub Enterprise

GitHub Enterprise-compatible targets can specify the host and optional API URL:

```bash
shiki init /path/to/repo \
  --repo OWNER/REPO \
  --github-host github.example.com \
  --github-api-url https://github.example.com/api/v3 \
  --remote-protocol ssh
```

If `--github-api-url` is omitted for a non-GitHub.com host, Shiki derives:

```text
https://HOST/api/v3
```

Shiki still uses the GitHub CLI (`gh`) as the backend. For enterprise hosts,
Shiki injects `GH_HOST=HOST` into relevant `gh` commands instead of mutating the
global process environment. Operators must authenticate `gh` for that host.

## Repository Mirror

Executed `start` / `init` writes provider fields to `.shiki/repo.json`:

```json
{
  "source_of_truth": "github",
  "provider": "github",
  "repo": "OWNER/REPO",
  "host": "github.com",
  "remote_protocol": "https",
  "web_base_url": "https://github.com",
  "api_base_url": "https://api.github.com",
  "ssh_host": "github.com",
  "canonical_remote_url": "https://github.com/OWNER/REPO.git",
  "default_branch": "main",
  "mirror": ".shiki"
}
```

Legacy records without provider fields remain valid and are interpreted as
GitHub.com HTTPS.

## Not Supported

- Non-GitHub providers.
- GitLab or Bitbucket.
- Provider plugin loading.
- A replacement for the GitHub CLI.
- Full GitHub Enterprise policy discovery or doctor checks. Those belong to the
  P1.3.5 doctor task.
