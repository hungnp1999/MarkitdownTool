# MarkitdownTool 2.2.0

A Windows desktop tool for processing PDF, DOCX, PPTX, and XLSX files. Scanned PDFs can be OCR-processed with Tesseract and OCRmyPDF.

---

## Author

Nguyễn Phú Hùng

GitHub:
https://github.com/hungnp1999/MarkitdownTool

---

## License

This project is licensed under the MIT License.

See LICENSE.md for details.

---

## Disclaimer

Please review DISCLAIMER.md before using the software.

---

## Privacy

Please review PRIVACY.md for information regarding local processing and data handling.

---

## Third-Party Components

Please review THIRD_PARTY_NOTICES.md for information regarding third-party dependencies and licenses.

See [CHANGELOG.md](CHANGELOG.md) for versioned release notes.

## Processing modes

- **OCR only** creates searchable PDFs from scanned PDFs.
- **Markdown only** converts eligible documents without running OCR. The app checks the selected files first and asks whether to continue when some files cannot be converted.
- **OCR then Markdown** OCR-processes scanned PDFs and creates Markdown for supported documents.

Processing runs in the background so the window remains responsive. Cancel stops the batch after the current file finishes. Results, skipped files, and errors are reported separately.

## Adding documents

Use **Add files** to pick individual documents, or **Add folder** to add every supported document (PDF, DOCX, PPTX, XLSX) in a folder at once. When the folder has subfolders, MarkitdownTool asks whether to include them. Unsupported files and Office lock files (`~$...`) are ignored, and files already in the list are not added twice.

## Integrations: CLI, HTTP API, and MCP

MarkitdownTool can be connected to other components of the Human-centric Automation Ecosystem through three interfaces. All of them run locally, share the OCR language settings and output folders configured in the app, and return the same JSON result shape. **Settings → Connection** shows ready-to-copy commands for the current installation.

The installer places the console executable `markitdowntool.exe` next to `markitdown_gui.exe` in `%LOCALAPPDATA%\Programs\MarkitdownTool`. From source, use `python markitdown_cli.py` instead of `markitdowntool`.

### CLI

```powershell
markitdowntool convert "D:\Docs" --recursive --mode markdown --json
markitdowntool convert scan.pdf report.docx --mode ocr_markdown --lang vie+eng --output-dir "D:\Results"
markitdowntool classify "D:\Docs" --recursive
markitdowntool languages
markitdowntool info --json
```

- `--mode`: `ocr` (searchable PDF only), `markdown` (no OCR), or `ocr_markdown` (default).
- `--json` prints a machine-readable result; add `--include-content` to embed the Markdown text.
- `--output-dir` must be absolute; results go to its `markdown\` and `ocrpdf\` subfolders.
- Exit codes: `0` success (skipped files are not failures), `1` at least one file failed or the batch was cancelled, `2` the request could not run (missing path, OCR runtime, or language).

### HTTP API

```powershell
$env:MARKITDOWNTOOL_API_TOKEN = "change-me"
markitdowntool api --port 8765
```

| Method | Endpoint | Body |
| --- | --- | --- |
| GET | `/v1/health` | — (no token required) |
| GET | `/v1/info` | — |
| GET | `/v1/languages` | — |
| POST | `/v1/classify` | `{"paths": ["D:\Docs"], "recursive": true}` |
| POST | `/v1/convert` | `{"paths": [...], "mode": "markdown", "languages": ["vie", "eng"], "output_dir": "D:\Results", "recursive": false, "include_content": true}` |

`/v1/convert` also accepts uploads as `"files": [{"name": "report.docx", "content_base64": "..."}]` for callers that do not share the file system. Paths must be absolute paths on the machine running the server. Errors use `{"error": {"code", "message", "details"}}` with HTTP 400/401/404/405/413/503.

The server binds to `127.0.0.1` by default. When a token is set (`--token` or `MARKITDOWNTOOL_API_TOKEN`), every endpoint except `/v1/health` requires `Authorization: Bearer <token>`. Binding to a non-loopback address is refused without a token, because API callers can ask the server to read local files. Conversions run one batch at a time.

### MCP

`markitdowntool mcp` runs a Model Context Protocol server over stdio with the tools `convert_documents`, `classify_documents`, `list_ocr_languages`, and `get_tool_info`. Add it to an MCP host configuration, for example:

```json
{
  "mcpServers": {
    "markitdowntool": {
      "command": "C:\Users\<you>\AppData\Local\Programs\MarkitdownTool\markitdowntool.exe",
      "args": ["mcp"]
    }
  }
}
```

`convert_documents` returns the Markdown text by default (`include_content: true`), so agents can use the result directly.

## OCR languages

Use **OCR Languages** to review installed language packages. Packages installed by MarkitdownTool are downloaded from the official `tesseract-ocr/tessdata_fast` repository and stored in the application's `tesseract/tessdata` folder. They can be removed from the app there. Languages supplied by a system Tesseract installation are read-only and are not modified by MarkitdownTool.

The installer bundles `eng`, `vie`, and `osd`, so those packages are available offline. Additional packages require an internet connection. Language models are stored in the user data directory and do not modify a system Tesseract installation.

## Run from source

Requirements: Windows 10/11 x64, Python 3.12, and Tesseract OCR with the required language data installed.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python markitdown_gui.py
```

The application opens a persistent work window. Add files or a folder, select a processing mode, and start a batch. By default, configuration, output, and logs are stored under `%LOCALAPPDATA%\MarkitdownTool`; the user data directory can be changed in Settings. The executable and bundled OCR runtime are installed under `%LOCALAPPDATA%\Programs\MarkitdownTool`.

## Build the executable

```powershell
python -m PyInstaller --noconfirm .\markitdown_gui.spec
```

The executables are created at `dist\markitdown_gui.exe` (desktop app) and `dist\markitdowntool.exe` (CLI, HTTP API, and MCP server).

## Build the installer

Install Tesseract OCR with the `eng`, `vie`, and `osd` language data, and install NSIS 3. Then run:

```powershell
.\installer\Build-Release.ps1
```

The script stages the Tesseract runtime, builds the executable, and creates `dist\MarkitdownTool-Setup.exe`. The installer targets the current Windows user and does not require administrator rights.

## Repository contents

Generated binaries, build intermediates, virtual environments, runtime output, logs, and user configuration are excluded from Git. The installer is distributed as a GitHub Release asset rather than committed to the repository.
