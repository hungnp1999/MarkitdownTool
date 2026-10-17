# CLAUDE.md

Guidance for Claude Code sessions working on MarkitdownTool. Keep this file short; put task state in GitHub issues and PRs, not here.

## Conventions

- Talk to the maintainer in **Vietnamese**. Write code, comments, commit messages, PR and issue text, README and CHANGELOG in **English**.
- UI strings live in `i18n.py` as `(english, vietnamese)` pairs; every new key needs both (enforced by `tests/test_i18n.py`).
- Match the surrounding style: standard library first, small functions, few comments.

## Architecture

| Module | Role |
| --- | --- |
| `document_core.py` | Core pipeline: `classify_file`, `collect_supported_files`, `run_ocr` (OCRmyPDF + Tesseract), `convert_markdown` (MarkItDown), `process_batch`. `SUPPORTED_EXTENSIONS` gates every interface. |
| `app_paths.py`, `app_runtime.py` | Version (`APP_VERSION`), config file, per-user data folders, logging, API token file. Shared by GUI and integrations. |
| `integration_service.py` | Protocol-neutral service returning JSON-ready dicts; **all path security lives here** (`allowed_roots`, `ensure_allowed_path`). |
| `markitdown_cli.py` | CLI (`convert`, `classify`, `languages`, `info`, `api`, `mcp`); exit codes 0/1/2. |
| `api_server.py` | Local HTTP API (stdlib `http.server`), bearer token always required, loopback Host and JSON content-type checks. |
| `mcp_server.py` | MCP stdio server (`mcp` 2.x, `MCPServer`, structured output). |
| `markitdown_gui.py` | Tkinter GUI; Settings → Connection shows CLI/API/MCP snippets. |
| `markitdown_gui.spec`, `installer/` | PyInstaller (two onefile exes: `markitdown_gui.exe`, console `markitdowntool.exe`) and NSIS installer. |

## Security rules (do not regress)

- Every caller-supplied path (input, folder, `output_dir`) must go through `ensure_allowed_path` before any `pathlib`/file-system call. Before that point use string-only `os.path` functions.
- Keep the final check in `ensure_allowed_path` as `realpath` followed by an **inline** `startswith` guard on the same variable; CodeQL `py/path-injection` does not recognise checks hidden in helper functions.
- UNC/WebDAV/device paths must be refused before any I/O (NTLM leak).
- Open security follow-ups: issues #6–#9.

## Commands (Windows, run from the repo root)

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests      # all tests
.\.venv\Scripts\python.exe markitdown_cli.py info              # CLI from source
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm .\markitdown_gui.spec   # exes only
.\installer\Build-Release.ps1                                   # exes + installer
```

- The venv uses **Python 3.12** (`py -3.12 -m venv .venv`). Don't use the machine's default Python 3.15: `markitdown` requires `<3.15`.
- GitHub CLI: `C:\Program Files\GitHub CLI\gh.exe` (may not be on PATH in older sessions).
- Release build needs Tesseract at `C:\Program Files\Tesseract-OCR` (installed via winget, has only `eng` + `osd`) and NSIS. Before `Build-Release.ps1`, copy `vie.traineddata` into `build\installer_payload\tesseract\tessdata\` (a copy exists in `%LOCALAPPDATA%\Programs\MarkitdownTool\tesseract\tessdata\`); the script requires it.

## Tooling pitfalls

- In the Bash tool, `\\` collapses to `\` even inside quoted heredocs. Write any text containing Windows paths or backslashes (docstrings, README, PR bodies, release notes) with the Write/Edit tools, then pass files with `--body-file` / `--notes-file`.
- Testing the onefile exes: a killed parent leaves the child holding pipes. Do not pipe the server's stderr in smoke tests; stop it with `taskkill /T`.
- `collect_all('mcp')` must exclude `mcp.cli` (needs `typer`); see the spec.

## Workflow

1. Branch from up-to-date `main`: `feature/*`, `fix/*`, `docs/*`.
2. Update `CHANGELOG.md` (an `[Unreleased]` section until release) and README when behaviour changes.
3. Run the full test suite; for interface changes also smoke-test the CLI/API/MCP.
4. Open a PR with `gh pr create --body-file ...`; the maintainer approves merges.
5. CodeQL (default setup) on a PR only reports alerts on changed lines. After merging, confirm the `main` analysis of the merge commit with the `codeql-verify` skill (`.claude/skills/codeql-verify/`).
6. Release (step by step in the `release` skill, `.claude/skills/release/`): bump `APP_VERSION` (`app_runtime.py`), `PRODUCT_VERSION`/`VIProductVersion` (`installer/MarkitdownTool.nsi`) and the README title; build from `main`; smoke-test `dist\markitdowntool.exe`; `gh release create vX.Y.Z` with `MarkitdownTool-Setup.exe`, `markitdown_gui.exe`, `markitdowntool.exe` and their SHA-256 in the notes.

## Backlog

- #6–#9: security follow-ups (TOCTOU/junctions, resource limits, MCP scope and allowed-folders UI, token management).
- #11: more input formats (`.doc`, `.xls`, others). Solution not yet agreed; discuss before coding.
