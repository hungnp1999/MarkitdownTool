# Changelog

Notable changes to MarkitdownTool are listed here by release.

## [Unreleased]

### Added

- HTML (`.html`, `.htm`), EPUB (`.epub`), plain text (`.txt`), Markdown (`.md`), JSON (`.json`), and XML (`.xml`) files convert to Markdown in the app, CLI, API, and MCP server. Remote images in HTML are kept as links and never downloaded (#11).
- The app converts legacy Office and OpenDocument files (`.doc`, `.rtf`, `.odt`, `.ppt`, `.odp`, `.ods`) to Markdown through the installed Microsoft Word, PowerPoint or Excel. Each file is copied to a private temporary folder, opened read-only with macros disabled, and saved as `.docx`/`.pptx`/`.xlsx` first. An Office app that does not respond within 60 seconds is stopped (only the instance this conversion started) and the file is marked failed (#11).

### Changed

- The CLI, HTTP API, and MCP server report legacy Office and OpenDocument files (`.doc`, `.rtf`, `.odt`, `.ppt`, `.odp`, `.ods`) with `reason_code: office_gui_only` instead of `unsupported_format`. These formats will be converted only in the app, through Microsoft Office, because Office can reach the network while it opens a file (ADR-011, #11). Without the matching Office app the app reports them with `needs_office`.

## [2.3.0] - 2026-10-17

### Added

- **Settings → Security** (new tab) with an **Allowed folders** list for the CLI, API, and MCP server, saved as `allowed_folders` in `config.json` and used when `MARKITDOWNTOOL_ALLOWED_ROOTS` is not set. `info` / `get_tool_info` report `allowed_roots` and `allowed_roots_source` (#8).
- API token management: **Copy token** and **Rotate token** in Settings → Security, and `markitdowntool api-token [--rotate] [--json]`, which creates or rotates the token and prints the token file's path, never the token itself. A running API server picks up a rotated token on its next request (#9).
- Audit log of API requests (client address, method, endpoint, status, file count, duration) in the session log, without paths, content, or tokens (#9).
- Wrongly encoded PDF text layers are detected. Each PDF gets a `text_quality` score (0–1, the share of words that look correctly encoded). Pages below 0.8 are reported with `reason_code: garbled_text` and `low_quality_pages`, and the OCR modes OCR them again with their text layer replaced, keeping the other pages unchanged. This recovers documents made with legacy Vietnamese fonts (TCVN3), fonts without a Unicode map, and poor earlier OCR. Previously these PDFs counted as having text, so OCR skipped them.
- `--force-ocr` (CLI) and `force_ocr` (API, MCP) OCR every PDF again and replace its text layer, for damage the check does not catch. `convert` results report `force_ocr`; the manifest reports `text_quality_threshold`.
- Legacy Excel 97–2003 (`.xls`) and CSV (`.csv`) files convert to Markdown in the app, CLI, API, and MCP server (#11).
- Images (`.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`, `.bmp`) are OCR-processed into searchable PDFs and Markdown in the OCR modes. Previously **Add files** listed them but every batch skipped them (#11). Images without resolution metadata are processed at 300 DPI, and transparent images are flattened onto white, because OCRmyPDF refuses both otherwise. In **Markdown only** mode images are skipped with `reason_code: needs_ocr`.
- The manifest's `formats` entries include `kind` (`document` or `image`).
- `markitdowntool manifest`, `GET /v1/manifest`, and the MCP tool `get_manifest` describe the installation's capabilities: interfaces, supported formats, modes, installed OCR languages, engine versions (MarkItDown, OCRmyPDF, pypdfium2, Tesseract), limits, and schema URLs.
- JSON Schemas for the `manifest`, `info`, `classify`, `convert`, and `error` outputs in `schemas/`, with a compatibility rule for `schema_version`.
- `classify` and `convert` results include the source file's `size` and `sha256`.
- `classify` results include `schema_version`.

### Changed

- **Markdown only** now skips PDFs whose text layer is wrongly encoded (`garbled_text`) instead of converting the wrong text; use an OCR mode for them.
- Folder inputs in the CLI, API, and MCP server now list unsupported files too (`supported: false`, or skipped with `reason_code: unsupported_format`) instead of dropping them. Hidden and system files, Office lock files, and unsupported files inside hidden folders are still left out. A folder with only unsupported files is no longer a `no_input_files` error.

### Security

- The generated API token file is written with an explicit ACL for the current user and SYSTEM only, instead of the inherited `%LOCALAPPDATA%` ACL. The file is restricted before it replaces the old one, and `icacls`/`whoami` are run from System32 by full path. Existing tokens get the ACL when they are rotated (#9).
- Integrations now read junctions and symlinks along a path without following them and refuse links to network shares or devices before `realpath` opens them, so a link inside an allowed folder can no longer make Windows contact a remote host (#6).
- Recursive folder scans in the CLI, API, and MCP server no longer enter junctions or symlinked folders (#6).
- Each file is opened once and the opened handle's real path is checked against the allowed folders before OCR or Markdown conversion reads it, closing the window in which a folder could be swapped for a junction after the path check (#6).
- Added resource limits for integration requests: `max_files`, `max_scan_depth`, `max_content_bytes`, and `batch_wait_seconds`, configurable under `integration_limits` in `config.json` and shown by `info`. Exceeding one returns `limit_exceeded` (HTTP 413); waiting too long for a running batch returns `busy` (HTTP 503). Markdown beyond `max_content_bytes` is left out of the response with `"content_omitted": "limit_exceeded"` (#7).

### Changed

- Rewrote the final allowed-folder check in `ensure_allowed_path` as an inline `realpath` + `startswith` guard so CodeQL recognises it as a path sanitiser. Behaviour is unchanged; allowed roots are now stored in canonical letter case.

## [2.2.1] - 2026-10-16

### Security

- CLI, HTTP API, and MCP integrations now only accept paths on local drives. UNC and WebDAV paths, mapped network drives, device paths, and junctions or symlinks that leave the allowed folders are refused before any file access. This prevents NTLM credential leaks through attacker-supplied paths, for example via prompt injection against an MCP agent. Resolves CodeQL `py/path-injection` alerts in `integration_service.py`.
- Added `MARKITDOWNTOOL_ALLOWED_ROOTS` to restrict integrations to specific folders.
- The HTTP API now always requires a bearer token. Without `--token` or `MARKITDOWNTOOL_API_TOKEN`, a random token is generated in the per-user settings folder, so other accounts on the same machine cannot use the API.

## [2.2.0] - 2026-10-16

### Added

- Added **Add folder** next to **Add files** to add every supported document in a folder, optionally including subfolders.
- Added integration interfaces for the Human-centric Automation Ecosystem, sharing one JSON result format:
  - CLI (`markitdowntool convert | classify | languages | info`) with `--json` output and documented exit codes.
  - Local HTTP API (`markitdowntool api`) with bearer-token authentication, base64 uploads, and loopback-only binding unless a token is set. On loopback it rejects non-loopback `Host` headers and non-JSON POST bodies so web pages cannot reach it through DNS rebinding or cross-site form posts.
  - MCP server over stdio (`markitdowntool mcp`) with `convert_documents`, `classify_documents`, `list_ocr_languages`, and `get_tool_info` tools.
- Added the console executable `markitdowntool.exe` to the build and installer.
- Settings → Connection now shows ready-to-copy CLI, API, and MCP configuration for the current installation.

### Changed

- Moved shared configuration and data-path setup into `app_runtime.py` so the GUI and integrations use the same settings.
- Updated GitHub links to the repository's new owner: https://github.com/hungnp1999/MarkitdownTool.

## [2.1.2] - 2026-10-08

### Changed

- Updated the PDFium binding `pypdfium2` from 5.13.0 to 5.14.0.

## [2.1.1] - 2026-10-08

### Changed

- Streamlined the main window by removing the separate OCR language manager shortcut. Language package management remains available in Settings and OCR configuration.

## [2.1.0] - 2026-10-08

### Added

- Added a persistent GUI with OCR-only, Markdown-only, and OCR-then-Markdown processing modes.
- Added English and Vietnamese UI localization, Settings groups, release checking, and AI connection scaffolding for future MCP, CLI, and agent integrations.
- Added OCR language package search, installation from the official Tesseract `tessdata_fast` repository, and removal of app-managed packages.
- Added structured per-file results, background processing, cooperative cancellation between files, and processing logs.
- Added per-user install and data paths, legacy data migration, a versioned installer, and selectable uninstaller components.
- Added third-party license, privacy, and disclaimer documentation.

### Changed

- Replaced PyMuPDF in PDF text-layer detection with PDFium through `pypdfium2`; Microsoft MarkItDown continues to use its PDF conversion dependencies.
- Moved configuration, output, logs, and downloaded OCR language data to the user data directory by default.
