---
name: codeql-verify
description: Verify MarkitdownTool's CodeQL code scanning - the analysis on a pull request, the analysis on main (including the merge commit of a merged PR), and every open code-scanning alert - and report a clear verdict with what to do next. Use this whenever the maintainer asks whether CodeQL/code scanning is clean, green or passing, whether a PR is safe to merge security-wise, whether merging introduced alerts, what alerts are still open, or to check "analysis" on a PR or on main (e.g. "CodeQL của PR 14 sao rồi", "kiểm tra code scanning trên main", "còn cảnh báo nào mở không", "merge xong check lại CodeQL"), and also after merging any PR that touches Python code and before a release.
---

# Verify CodeQL on a PR and on main

MarkitdownTool uses GitHub code scanning **default setup** (Python, `default` suite, weekly schedule plus every push and PR). The trap this skill exists for: **a PR's CodeQL check only reports alerts on the lines the PR changed.** A green "CodeQL: No new alerts in code changed by this pull request" doesn't mean `main` stays clean after the merge. PR #5 is the real example: its PR analysis had 0 results, and the analysis of its merge commit on `main` immediately raised 8 high-severity `py/path-injection` alerts in `document_core.py`, a file the PR did not touch. So the check is not done until the analysis of the **merge commit on main** is in.

Talk to the maintainer in Vietnamese. Everything here is read-only. Dismissing an alert, re-running a workflow or changing code-scanning settings changes the repository's security record, so do those only after the maintainer says yes to that specific action.

## The report script

```powershell
.\.claude\skills\codeql-verify\scripts\codeql_status.ps1 [-Pr N] [-Branch main]
```

Run it as its own process if you capture its output from Bash (`powershell.exe -NoProfile -ExecutionPolicy Bypass -File ...`). It prints:

- the default-setup state;
- for `-Pr N`: the CodeQL and `Analyze (python)` check runs on the PR head, the newest PR analysis (and whether it is for the current head commit), CodeQL workflow runs for that commit, and open alerts on the PR ref. If the PR is merged, it also prints the `main` analysis of the merge commit and **the alerts first raised by that analysis**, with their current state;
- for the branch: its head, the newest analysis (current or older than head), runs in progress, and every open alert with severity, rule, `file:line`, message and link;
- a verdict line and exit code: `CLEAN` (0), `ALERTS` (1), `PENDING` or `STALE` (2).

## Pick the situation

**Before merging a PR.** Run with `-Pr N`. Report the PR check and analysis. A PR verdict of CLEAN means "no alerts on changed lines" and nothing more, so say so. If the PR touches `integration_service.py`, `api_server.py`, `mcp_server.py`, `document_core.py` or any code that handles caller-supplied paths, read the diff against the security rules in `CLAUDE.md` (every caller path goes through `ensure_allowed_path` before any `pathlib` or file-system call, an inline `realpath` + `startswith` guard, UNC paths refused before I/O). Point out anything that could raise alerts in unchanged code that the PR now feeds. Code scanning here covers **Python only**. If the PR changes other code (PowerShell scripts, the NSIS installer, the PyInstaller spec), say plainly that CodeQL did not analyse those files and that a green check says nothing about them. Finish with: re-check after merging.

**After merging.** Run with `-Pr N` again. If the merge-commit analysis is missing and a run is in progress, the verdict is PENDING: wait for that run in the background (`gh run watch <run-id> --exit-status`, using the run URL's id) and then rerun the script once. Don't loop or poll. If it raised alerts, that's the headline even when they were later fixed: name them, and say whether they're still open. When they were fixed later, find the fixing commit (the first `main` analysis after the merge with the alerts gone) and check whether a release tag sits in between: `git tag --contains <merge-sha> --no-contains <fix-sha>`. If one does, read the fix (its PR and CHANGELOG entry) before giving advice. A fix that only rewrote a guard so CodeQL recognises it (behaviour unchanged, as with PR #10) means the release is not affected. A fix that changed behaviour means the release shipped the vulnerable code, and the maintainer should hear that with a suggested patch release.

**State of main / open alerts.** Run without `-Pr`. For each open alert, open it (`gh api repos/hungnp1999/MarkitdownTool/code-scanning/alerts/<n>`; `/instances` for history), read the code at the location, and explain in a sentence or two why CodeQL flags it and what a fix looks like in this codebase. For `py/path-injection` the fix is almost always a missing or non-inline guard, see `CLAUDE.md`. Link related GitHub issues if there are any (`gh issue list --search "<rule or file>"`). Don't start fixing unless asked; offer a `fix/*` branch.

**STALE** (newest analysis is for an older commit and nothing is running): check `gh run list --workflow "CodeQL" --limit 5` (default-setup runs are named "Push on main" / "PR #N") for a failed or cancelled run. Report it, and offer to re-run it (`gh run rerun <id>`) once the maintainer agrees.

**Analysis errors** (`ERROR:` in the analysis line, or a failed `Analyze (python)` job): show the job log tail (`gh run view <id> --log-failed`). This means "unknown", not "clean"; say so.

## Reply format

Keep it short and lead with the verdict. Use this shape, in Vietnamese:

```
**CodeQL: <SẠCH | CÓ CẢNH BÁO | ĐANG CHẠY | CHƯA CẬP NHẬT>**

- PR #N: <check result>, analysis <commit> (<n> kết quả) - chỉ tính dòng PR sửa.
- main @ <commit>: analysis <current/older>, <n> cảnh báo mở.
- Merge commit <sha>: <n> cảnh báo mới phát sinh (<now open/fixed>)   <- only for merged PRs

<table of open alerts: #, mức độ, rule, file:dòng, lý do ngắn>   <- only if any

Bước tiếp theo: <one concrete next step, e.g. "kiểm tra lại sau khi merge", "sửa #15 trên nhánh fix/...", "chờ run 3901... xong">
```

Name what you could not verify (an analysis still running, a run that failed) instead of rounding it to "clean".
