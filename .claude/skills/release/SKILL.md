---
name: release
description: Ship a new MarkitdownTool version end to end - merge the approved PR(s), bump the version through a release PR, build the exes and installer from main, smoke-test the binaries, compute SHA-256 hashes, tag, and publish the GitHub release with the three assets. Use this whenever the maintainer wants to release, publish, ship, cut, or tag a version (e.g. "release 2.3.0", "ra bản mới", "phát hành v2.2.2", "đóng gói và đưa lên GitHub Releases", "build installer rồi release"), and also when they ask for only part of this chain on a release (rebuild the release exes, recompute release hashes, redo a failed release, finish a draft release).
---

# Release MarkitdownTool

The chain is **merge → version bump PR → build → check exes → SHA-256 → tag → release**. Each step guards the next: a release must be built from the exact `main` commit that the tag points to, the exes must show the version being released, and the hashes in the notes must match the uploaded files. Talk to the maintainer in Vietnamese; write commits, PR text, CHANGELOG and release notes in English (see `CLAUDE.md`).

Repository: `hungnp1999/MarkitdownTool`. Run everything from the repo root in PowerShell. The helper scripts are in `.claude\skills\release\scripts\`.

## Stop and ask the maintainer at three points

These are outward-facing and hard to undo, so stop and wait for an explicit yes before each one, even if the release was requested in one sentence:

1. **Before every merge** (feature PRs and the release PR). Show the PR title, number, review state and check results.
2. **Before pushing the tag** `vX.Y.Z`. Show the commit it will point to and the smoke test and hash results.
3. **Before publishing** the release. Create it as a draft first, show its URL and notes, then publish.

Everything else (branching, version bump, build, tests, hashing, the draft) can go ahead without asking. If a step fails, stop, explain in Vietnamese what failed and what you suggest; don't work around a failed check to keep the release moving.

## 0. Preflight

- Working tree clean and on `main`, up to date with `origin/main` (`git status -sb`, `git fetch`).
- Tools: `gh` (on PATH via winget; `gh auth status`), the project venv `.venv\Scripts\python.exe` (Python 3.12, not the machine's 3.15), NSIS (`%LOCALAPPDATA%\Programs\NSIS\makensis.exe`), Tesseract at `C:\Program Files\Tesseract-OCR`.
- Decide the version `X.Y.Z`. If the maintainer didn't give one, read `CHANGELOG.md` `[Unreleased]` and the latest tag (`gh release list -L 3`) and propose one: **patch** for fixes and security fixes, **minor** for new features (`### Added`), **major** only if they ask. The new tag must not exist yet (`git ls-remote --tags origin vX.Y.Z`). Say which version you picked and why; include it in the first stop question.
- Stop now if `[Unreleased]` is empty: there is nothing to release.

## 1. Merge the approved PR(s)

For each PR the maintainer named (or the open PRs they want in this release; `gh pr list` if unclear):

```powershell
gh pr view N --json number,title,state,isDraft,reviewDecision,mergeable,headRefName
gh pr checks N
```

A PR is ready when it is open, not a draft, `mergeable` is `MERGEABLE` and the checks have passed (CodeQL included). `reviewDecision` may be empty because the maintainer approves merges in chat; that's fine. → **Stop point 1.** Then merge the way earlier PRs were merged (merge commits):

```powershell
gh pr merge N --merge --delete-branch
```

If no PR needs merging (everything is already on `main`), say so and skip to step 2.

## 2. Version bump release PR

```powershell
git switch main; git pull --ff-only
git switch -c release/vX.Y.Z
.\.claude\skills\release\scripts\set_version.ps1 -Version X.Y.Z
.\.claude\skills\release\scripts\check_versions.ps1 -Expected X.Y.Z
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

The script updates `APP_VERSION` (`app_runtime.py`), `PRODUCT_VERSION` and `VIProductVersion` (`installer\MarkitdownTool.nsi`), the README title, and renames `## [Unreleased]` to `## [X.Y.Z] - <today>` in `CHANGELOG.md`. It refuses an empty `[Unreleased]` or an existing `[X.Y.Z]`. Check `git diff` shows only those five lines.

Commit (`Release X.Y.Z`), push the branch, and open the PR with a body written by the Write tool and passed with `--body-file` (backslashes get mangled through Bash). Wait for checks (`gh pr checks N --watch`). → **Stop point 1** again, then `gh pr merge N --merge --delete-branch`.

## 3. Build from main

```powershell
git switch main; git pull --ff-only
.\.claude\skills\release\scripts\check_versions.ps1 -Expected X.Y.Z
git rev-parse HEAD        # remember this: the tag must point here
```

Stage the Vietnamese OCR data (the winget Tesseract only has `eng` and `osd`; `Build-Release.ps1` fails without `vie`):

```powershell
New-Item -ItemType Directory -Force build\installer_payload\tesseract\tessdata | Out-Null
Copy-Item "$env:LOCALAPPDATA\Programs\MarkitdownTool\tesseract\tessdata\vie.traineddata" build\installer_payload\tesseract\tessdata\ -Force
```

Note the start time (`$buildStart = Get-Date`, and keep it for step 4), then run the build **in the background** (PyInstaller plus NSIS takes several minutes) as its own PowerShell process, with output going to a log file:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\installer\Build-Release.ps1 > <scratchpad>\build.log 2>&1
```

Don't run the script in the current Windows PowerShell 5.1 session with `*>` or `2>&1`: in that session, PyInstaller's `INFO` lines on stderr become error records, and the script's `$ErrorActionPreference = "Stop"` aborts the build at its first log line. It produces `dist\markitdown_gui.exe`, `dist\markitdowntool.exe` and `dist\MarkitdownTool-Setup.exe`. Older builds may still sit in `dist\`, which is why step 4 checks timestamps.

## 4. Check the exes

```powershell
.\.claude\skills\release\scripts\smoke_test.ps1 -Version X.Y.Z -BuiltAfter $buildStart
```

It checks that all three files exist and are newer than the build start, that `markitdowntool.exe --version` and `info --json` report `X.Y.Z`, that the console exe converts a generated `.xlsx` to Markdown, that `markitdown_gui.exe` starts and stays up for 10 s (then it is killed with `taskkill /T`), and that the installer's ProductVersion is `X.Y.Z`. A GUI window flashes up briefly during the run; mention that before running it. If the release adds an input format or interface, also run that feature once through `dist\markitdowntool.exe` (for example convert a sample of the new format), because the frozen exe can miss modules that the source tests don't need. Any FAIL stops the release: report the table. The fix goes through a normal fix PR; nothing has been tagged yet, so rebuild the same version afterwards.

## 5. SHA-256

```powershell
.\.claude\skills\release\scripts\hash_artifacts.ps1 -OutFile <scratchpad>\sha256-X.Y.Z.md
```

This prints the `## SHA-256` block (uppercase hex, Setup → GUI → CLI), the same format as earlier releases.

## 6. Tag

Confirm `git rev-parse HEAD` still equals the commit from step 3 and `main` has not moved (`git fetch; git status -sb`). → **Stop point 2.** Then:

```powershell
git tag vX.Y.Z <commit>
git push origin vX.Y.Z
```

Earlier tags are lightweight tags on the merge commit; keep doing that.

## 7. Release notes and draft release

Write the notes with the Write tool into the scratchpad (`release-notes-X.Y.Z.md`). Base them on the `[X.Y.Z]` CHANGELOG section, written for users, not developers:

```markdown
## <One-line theme, e.g. "Security release" or "New: Add folder">

<Who should update, in one sentence, if it matters.>

- **<Change in user terms>.** <What it means for them.>
- ...
- **Breaking change:** <only if there is one, with what to do>.

See CHANGELOG.md for the full history.

<the ## SHA-256 block from step 5, unchanged>
```

```powershell
gh release create vX.Y.Z --verify-tag --draft --title "MarkitdownTool vX.Y.Z" --notes-file <notes> `
  dist\MarkitdownTool-Setup.exe dist\markitdown_gui.exe dist\markitdowntool.exe
```

Then check that GitHub got the same bytes: `gh release view vX.Y.Z --json assets --jq '.assets[] | [.name, .digest] | @tsv'` returns `sha256:<hex>` per asset; compare each (case-insensitively) with step 5. → **Stop point 3** (show the draft URL). Then:

```powershell
gh release edit vX.Y.Z --draft=false --latest
```

## 8. Wrap up

- Report in Vietnamese: version, release URL, tag commit, the three hashes, smoke test result.
- Remind the maintainer that new PRs start a fresh `## [Unreleased]` section in `CHANGELOG.md`.
- If the release contains security fixes, check the `main` CodeQL analysis: `gh api "repos/hungnp1999/MarkitdownTool/code-scanning/alerts?state=open" --jq length`.

## Rebuilding the assets of a published release

When the maintainer reports a broken download or asks to replace assets on an existing `vX.Y.Z`:

1. **Diagnose first.** Compare the notes' SHA-256 with GitHub's digests (`gh release view vX.Y.Z --json assets`). If they match, the upload is intact, and rebuilding the same commit will most likely behave the same, so find the cause before rebuilding. Common causes: a console exe double-clicked with no arguments (exits at once with code 2), or SmartScreen blocking an unsigned download. Downloading the published assets to reproduce the problem needs the maintainer's OK (state the names and sizes).
2. **Build from the tag's commit, not `main`.** `main` has usually moved on, and building it would ship unreleased code under the old label. Use a detached worktree outside the repo, link the venv into it, and call the scripts by absolute path, because `.claude\` may not exist at the tag:
   ```powershell
   git worktree add --detach <wt> vX.Y.Z
   New-Item -ItemType Junction -Path <wt>\.venv -Target <repo>\.venv | Out-Null
   ```
   Then stage `vie.traineddata` into `<wt>\build\...`, build, and run `smoke_test.ps1` and `hash_artifacts.ps1` with `<wt>` as the current folder (they find the root with `git rev-parse --show-toplevel`).
3. **Never move the tag.** If the fix needs a code change, it is a new patch release, not a rebuild.
4. PyInstaller builds aren't byte-reproducible, and the installer embeds both exes, so recommend replacing all three assets together. → **Stop** before touching the published release, showing the old and new hashes. Then run `gh release upload vX.Y.Z <files> --clobber` (this deletes the old files on GitHub, so keep the downloaded copies), check the digests, and update the notes. Swap the `## SHA-256` block and add a short dated "Re-uploaded binaries" note above it with the previous hashes.
5. Remove the junction, then the worktree (`git worktree remove <wt> --force`).

## Resuming a half-finished release

Work out where it stopped before doing anything: is the release PR merged (`gh pr list --state merged --search "Release X.Y.Z"`), does the tag exist (`git ls-remote --tags origin vX.Y.Z`), is there a draft (`gh release view vX.Y.Z`)? Continue from the first step that is not done. If the maintainer's account doesn't match the repo (e.g. they say the release PR is merged but `main` still has the old version), stop and show what you found before doing anything. If binaries must be rebuilt after the tag exists, build the tag's commit as described above; never move a published tag.
