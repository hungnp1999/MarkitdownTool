# CLAUDE.md

Guidance for Claude Code sessions working on MarkitdownTool. Keep this file short; put task state in GitHub issues and PRs, not here.

## Conventions

- Talk to the maintainer in **Vietnamese**. Write code, comments, commit messages, PR and issue text, README and CHANGELOG in **English**.
- UI strings live in `i18n.py` as `(english, vietnamese)` pairs; every new key needs both (enforced by `tests/test_i18n.py`).
- Match the surrounding style: standard library first, small functions, few comments.

## Architecture

Module map, main flows and invariants: `ARCHITECTURE.md` (update it in the same PR when they change). All path security lives in `integration_service.py` (`allowed_roots`, `ensure_allowed_path`); `SUPPORTED_EXTENSIONS` in `document_core.py` gates every interface.

## Security rules (do not regress)

- Every caller-supplied path (input, folder, `output_dir`) must go through `ensure_allowed_path` before any `pathlib`/file-system call. Before that point use string-only `os.path` functions.
- Keep the final check in `ensure_allowed_path` as `realpath` followed by an **inline** `startswith` guard on the same variable; static analysers and reviewers do not recognise checks hidden in helper functions.
- UNC/WebDAV/device paths must be refused before any I/O (NTLM leak).
- Open security follow-ups: issues #6–#9.

## Provenance

- Git and PR operations only in an environment listed in `docs/DEVELOPMENT_PROVENANCE.md` (`cloud`, `pc-01`); never on an undeclared device.
- Every commit ends with `Dev-Environment: <label>`; `.githooks/prepare-commit-msg` adds it once the SessionStart hook has run. Cloud commits also keep `Claude-Session:`. The `Provenance` workflow fails a PR otherwise.
- Merging through the API: put `Dev-Environment:` and `Claude-Session:` in the merge or squash commit message.

## Other AI agents

- Only Claude Code works in this repo; `AGENTS.md` tells any other agent to stop. The Antigravity trial was closed on 2026-11-29 (lessons: lfmap `docs/research/2026-11-29-antigravity-deputy-retrospective.md`).

## Commands (Windows, run from the repo root)

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests      # all tests
.\.venv\Scripts\python.exe markitdown_cli.py info              # CLI from source
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm .\markitdown_gui.spec   # exes only
.\installer\Build-Release.ps1                                   # exes + installer
```

- The venv uses **Python 3.12** (`py -3.12 -m venv .venv`). Don't use the machine's default Python 3.15: `markitdown` requires `<3.15`.
- GitHub CLI: `C:\Program Files\GitHub CLI\gh.exe` (may not be on PATH in older sessions).
- Release build prerequisites (Tesseract `vie` data, NSIS): the `release` skill and `docs/dev/workflow.md`.

## Tooling pitfalls

- In the Bash tool, `\\` collapses to `\` even inside quoted heredocs. Write any text containing Windows paths or backslashes (docstrings, README, PR bodies, release notes) with the Write/Edit tools, then pass files with `--body-file` / `--notes-file`.
- Testing the onefile exes: a killed parent leaves the child holding pipes. Do not pipe the server's stderr in smoke tests; stop it with `taskkill /T`.
- `collect_all('mcp')` must exclude `mcp.cli` (needs `typer`); see the spec.

## Workflow (full text: `docs/dev/workflow.md`)

1. Branch from up-to-date `main`: `feature/*`, `fix/*`, `docs/*`.
2. Update `CHANGELOG.md` (`[Unreleased]`) and README when behaviour changes.
3. Run the full test suite; for interface changes also smoke-test the CLI/API/MCP. Before merging a code PR that touches `integration_service.py`, the CLI/API/MCP interfaces, `office_converter.py`, subprocess or network calls, or `.claude/`, run `/security-review` and fix or answer its findings. Docs-only PRs skip it.
4. Open a PR with `gh pr create --body-file ...`. Merge only when `Security scan`, `Secret scan`, `Provenance` and CodeQL (while public) are green on the head; after merging Python changes, confirm the `main` CodeQL analysis with the `codeql-verify` skill. A `noqa: S...` needs a reason on the same line.
5. Before each release and after a large refactor, redraw the architecture diagram (Artifact page) and note any drift from `ARCHITECTURE.md`.
6. Release: follow the `release` skill (`.claude/skills/release/`).

## Backlog

- #6–#9: security follow-ups (TOCTOU/junctions, resource limits, MCP scope and allowed-folders UI, token management).
- #11: more input formats (`.doc`, `.xls`, others). Solution not yet agreed; discuss before coding.
