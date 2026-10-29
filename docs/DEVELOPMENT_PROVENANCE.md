# Development provenance

This repository records where every change was made. Each commit names the development environment it came from, commits are signed, and a CI check refuses commits from an environment that is not listed below. The record supports authorship claims and makes the history traceable in the way supply-chain practice expects.

The rules follow common open-source practice:

- **Git trailers** carry the record, the same mechanism as the Developer Certificate of Origin (`Signed-off-by:`) and `Co-authored-by:`. They stay in the commit, are covered by its signature, and survive squash merges.
- **Signed commits** verified by GitHub prove which key, and so which environment, created a commit.
- **SLSA v1.0** (Supply-chain Levels for Software Artifacts) and **in-toto** describe source and build provenance; this file is the source side. Release builds record their environment in the release notes next to the SHA-256 of each file.

## Rules

1. Git and pull request operations (commit, push, opening, reviewing and merging pull requests, releases) happen only in an environment listed below. Employer-owned devices are never used. Reading and commenting on GitHub in a browser creates no commit and is allowed from any device.
2. Every commit ends with a `Dev-Environment: <label>` trailer. Commits made in a Claude Code cloud session also keep their `Claude-Session: <url>` trailer, and commits written with an AI assistant keep their `Co-Authored-By:` trailer. Commits written by an AI agent outside Claude Code (Google Antigravity, branches `ag/*`) are made on a listed environment, signed with its key, and add `AI-Agent: antigravity/<model>`.
3. Commits are signed with a key registered on GitHub for that environment.
4. Pull requests fill in the "Development environment" section of the template.
5. A merge or squash through the GitHub API keeps the trailers of the merged commits and adds those of the environment that performed the merge. GitHub signs the merge commit itself.

## Environments

| Label | Environment | Signing | Allowed since |
| --- | --- | --- | --- |
| `cloud` | Claude Code on the web: an ephemeral container hosted by Anthropic, one per session. The `Claude-Session` trailer links the session. | SSH signature added by the Claude Code session | 2026-10-23 |
| `pc-01` | The owner's personal desktop computer (Windows), used directly and through Claude Code. | SSH signing key of the device, registered on GitHub | 2026-10-23 |

Labels do not identify hardware or people. The owner keeps the mapping from label to physical device in a private register outside this repository.

To add a device, give it the next label (`pc-02`, ...), add a row here with the date, add the label to `allowed` in `.github/scripts/check-provenance.sh`, and register its signing key on GitHub. A retired device keeps its row with an end date.

## How the trailer is added

`.githooks/prepare-commit-msg` adds `Dev-Environment` to every commit message: the value of the `DEV_ENVIRONMENT` environment variable, or `cloud` in a Claude Code cloud session. The Claude Code SessionStart hook (`.claude/hooks/session-start.sh`) enables the hook with `git config core.hooksPath .githooks`. Run that command once in a clone used without Claude Code.

Setting up a personal computer (PowerShell, once per device):

```powershell
[Environment]::SetEnvironmentVariable('DEV_ENVIRONMENT', 'pc-01', 'User')
ssh-keygen -t ed25519 -C "pc-01 commit signing" -f "$HOME/.ssh/git_signing_pc01"
git config --global gpg.format ssh
git config --global user.signingkey "$HOME/.ssh/git_signing_pc01.pub"
git config --global commit.gpgsign true
git config --global tag.gpgsign true
```

Then add the `.pub` file on GitHub under Settings, SSH and GPG keys, as a **Signing key**.

## Checks

The `Provenance` workflow runs on every pull request. It fails when a commit has no `Dev-Environment` trailer, names an environment that is not listed, or is a cloud commit without `Claude-Session`. Commits by bots such as Dependabot are skipped. It also fails when GitHub does not show a commit as verified, that is, signed with a key registered on GitHub.

To read the record locally:

```bash
git log --format='%h %ad %G? %(trailers:key=Dev-Environment,valueonly,separator=) %s' --date=iso
```

GitHub's "Verified" badge on a commit, or `gh api repos/OWNER/REPO/commits/SHA --jq .commit.verification`, confirms the signature.

## Earlier history

Commits made before 2026-10-23 have no `Dev-Environment` trailer. Cloud commits from that period still carry `Claude-Session` and a signature. The owner documents the environments of that period in the private register.
