# Workflow details

Moved from `CLAUDE.md` on 2026-11-29 so that file stays short; `CLAUDE.md` keeps the summary. The text below is unchanged except step 4 (merge rule).

## Steps

1. Branch from up-to-date `main`: `feature/*`, `fix/*`, `docs/*`.
2. Update `CHANGELOG.md` (an `[Unreleased]` section until release) and README when behaviour changes.
3. Run the full test suite; for interface changes also smoke-test the CLI/API/MCP.
   Before merging a code PR that touches `integration_service.py`, the CLI/API/MCP interfaces, `office_converter.py`, subprocess or network calls, or `.claude/`, run `/security-review` and fix or answer its findings. Docs-only PRs skip it.
4. Open a PR with `gh pr create --body-file ...`. A PR may be merged once its checks are green on the head and it is not labelled `gate:po` (owner rule, Project Instructions v4 and policy CS-03, 2026-11-29); before that date the maintainer approved every merge.
5. CodeQL (default setup) runs while the repo is public and on a PR only reports alerts on changed lines. After merging, confirm the `main` analysis of the merge commit with the `codeql-verify` skill (`.claude/skills/codeql-verify/`). Independently of CodeQL, the `Security scan` workflow (ruff `S` rules, `ruff.toml`) and `Secret scan` (gitleaks) must pass, and `/security-review` runs for the paths in step 3. A `noqa: S...` needs a reason on the same line. If the repo goes private, CodeQL and the `codeql-verify` skill are retired (GitHub Code Security is paid, lfmap #346) and nothing blocks a merge: check the PR checks yourself before merging.
6. Before each release and after a large refactor, redraw the architecture diagram (Artifact page) and note any drift from `ARCHITECTURE.md`.
7. Release (step by step in the `release` skill, `.claude/skills/release/`): bump `APP_VERSION` (`app_runtime.py`), `PRODUCT_VERSION`/`VIProductVersion` (`installer/MarkitdownTool.nsi`) and the README title; build from `main`; smoke-test `dist\markitdowntool.exe`; `gh release create vX.Y.Z` with `MarkitdownTool-Setup.exe`, `markitdown_gui.exe`, `markitdowntool.exe` and their SHA-256 in the notes.

## Release build prerequisites

- Release build needs Tesseract at `C:\Program Files\Tesseract-OCR` (installed via winget, has only `eng` + `osd`) and NSIS. Before `Build-Release.ps1`, copy `vie.traineddata` into `build\installer_payload\tesseract\tessdata\` (a copy exists in `%LOCALAPPDATA%\Programs\MarkitdownTool\tesseract\tessdata\`); the script requires it.
