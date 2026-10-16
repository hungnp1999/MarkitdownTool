# Changelog

Notable changes to MarkitdownTool are listed here by release.

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
