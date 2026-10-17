# MarkitdownTool 2.2.1

A Windows desktop tool for processing PDF, DOCX, PPTX, XLSX, XLS, and CSV files and images. Scanned PDFs and images can be OCR-processed with Tesseract and OCRmyPDF.

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

- **OCR only** creates searchable PDFs from scanned PDFs and images.
- **Markdown only** converts eligible documents without running OCR. The app checks the selected files first and asks whether to continue when some files cannot be converted.
- **OCR then Markdown** OCR-processes scanned PDFs and images and creates Markdown for supported documents.

## Supported formats

| Format | Extensions | Markdown |
| --- | --- | --- |
| PDF | `.pdf` | Directly, or after OCR when a page has no text |
| Word, PowerPoint, Excel | `.docx`, `.pptx`, `.xlsx` | Directly |
| Legacy Excel 97–2003 | `.xls` | Directly (cell values only; macros are never run) |
| CSV | `.csv` | Directly, as a table (UTF-8, UTF-8 with BOM, or UTF-16) |
| Images | `.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`, `.bmp` | Only through OCR (**OCR only** or **OCR then Markdown**) |

Images without resolution metadata, such as phone photos and screenshots, are OCR-processed at 300 DPI. Transparent images are placed on a white background first. Multi-page TIFF files become multi-page PDFs. Legacy `.doc` and `.ppt` files are not supported yet.

## Wrongly encoded PDF text

Some PDFs show correct text on screen but contain a wrong text layer, for example documents made with legacy Vietnamese fonts (TCVN3 `.VnTime`, which extracts as `Ngµy hiÖu chuÈn`), fonts without a Unicode map (`hi�u`), or a poor earlier OCR. MarkitdownTool scores each PDF page's text with `text_quality`: the share of words that look correctly encoded, from 0 to 1. The check looks for encoding damage, not spelling, so it works the same for any language.

Pages that score below 0.8 are treated like scanned pages:

- the file is reported with `reason_code: garbled_text`, its `text_quality`, and `low_quality_pages`;
- the OCR modes OCR those pages again, replacing their text layer, and keep the other pages as they are;
- **Markdown only** skips the file, because its text would be wrong.

When the check misses a damaged file, use `--force-ocr` (CLI) or `"force_ocr": true` (API and MCP) to OCR every page of every PDF in the request again.

Processing runs in the background so the window remains responsive. Cancel stops the batch after the current file finishes. Results, skipped files, and errors are reported separately.

## Adding documents

Use **Add files** to pick individual documents, or **Add folder** to add every supported document and image in a folder at once. When the folder has subfolders, MarkitdownTool asks whether to include them. Unsupported files and Office lock files (`~$...`) are ignored, and files already in the list are not added twice.

## Integrations: CLI, HTTP API, and MCP

MarkitdownTool can be connected to other components of the Human-centric Automation Ecosystem through three interfaces. All of them run locally, only read and write files on local drives (see [Allowed folders](#allowed-folders)), share the OCR language settings and output folders configured in the app, and return the same JSON result shape. **Settings → Connection** shows ready-to-copy commands for the current installation.

The installer places the console executable `markitdowntool.exe` next to `markitdown_gui.exe` in `%LOCALAPPDATA%\Programs\MarkitdownTool`. From source, use `python markitdown_cli.py` instead of `markitdowntool`.

### CLI

```powershell
markitdowntool convert "D:\Docs" --recursive --mode markdown --json
markitdowntool convert scan.pdf report.docx --mode ocr_markdown --lang vie+eng --output-dir "D:\Results"
markitdowntool classify "D:\Docs" --recursive
markitdowntool languages
markitdowntool info --json
markitdowntool manifest
```

- `--mode`: `ocr` (searchable PDF only), `markdown` (no OCR), or `ocr_markdown` (default).
- `--json` prints a machine-readable result; add `--include-content` to embed the Markdown text.
- `--force-ocr` OCRs every PDF again and replaces its text layer (see [Wrongly encoded PDF text](#wrongly-encoded-pdf-text)).
- `--output-dir` must be absolute; results go to its `markdown\` and `ocrpdf\` subfolders.
- `manifest` prints the installation's capabilities as JSON: interfaces, supported formats, modes, installed OCR languages, engine versions, limits, and schema URLs.
- Folder inputs list every file, including unsupported ones (reported as `supported: false` / skipped with `reason_code: unsupported_format`), so nothing disappears from the result. Hidden and system files, Office lock files (`~$...`), and unsupported files inside hidden folders are left out.
- Exit codes: `0` success (skipped files are not failures), `1` at least one file failed or the batch was cancelled, `2` the request could not run (missing path, OCR runtime, or language).

### HTTP API

```powershell
markitdowntool api --port 8765
```

| Method | Endpoint | Body |
| --- | --- | --- |
| GET | `/v1/health` | — (no token required) |
| GET | `/v1/manifest` | — |
| GET | `/v1/info` | — |
| GET | `/v1/languages` | — |
| POST | `/v1/classify` | `{"paths": ["D:\Docs"], "recursive": true}` |
| POST | `/v1/convert` | `{"paths": [...], "mode": "markdown", "languages": ["vie", "eng"], "output_dir": "D:\Results", "recursive": false, "include_content": true, "force_ocr": false}` |

`/v1/convert` also accepts uploads as `"files": [{"name": "report.docx", "content_base64": "..."}]` for callers that do not share the file system. Paths must be absolute paths on the machine running the server. Errors use `{"error": {"code", "message", "details"}}` with HTTP 400/401/404/405/413/503.

The server binds to `127.0.0.1` by default. Every endpoint except `/v1/health` requires `Authorization: Bearer <token>`, because other accounts and processes on the same machine can also reach loopback. Set the token with `--token` or `MARKITDOWNTOOL_API_TOKEN`; otherwise MarkitdownTool generates one on first start and stores it in `%LOCALAPPDATA%\MarkitdownTool\api_token`, which only your Windows account can read. Binding to a non-loopback address requires an explicit token. On loopback, requests must use a loopback `Host` header (`localhost`, `127.0.0.1`, or `[::1]`), and POST bodies must be sent as `Content-Type: application/json`; this keeps web pages in a browser from calling the API through DNS rebinding or cross-site form posts. Conversions run one batch at a time.

The generated token can be managed in **Settings → Security** (**Copy token**, **Rotate token**) or from the command line:

```powershell
markitdowntool api-token            # print the token
markitdowntool api-token --rotate   # replace it; add --json for {token, token_file, rotated}
```

A rotated token takes effect immediately: a running server rereads the token file when it changes, so the old token stops working without a restart. The token file is written with an explicit ACL that grants access only to your account and SYSTEM. Tokens created by older versions keep the inherited `%LOCALAPPDATA%` ACL until they are rotated.

Every API request except `/v1/health` is recorded in the session log (`logs\api_*.log`) as an audit line with the client address, method, endpoint, HTTP status, number of files, and duration, for example `client=127.0.0.1 method=POST endpoint=/v1/convert status=200 files=3 duration_ms=5120`. Paths, document content, and tokens are never logged.

### MCP

`markitdowntool mcp` runs a Model Context Protocol server over stdio with the tools `convert_documents`, `classify_documents`, `list_ocr_languages`, `get_tool_info`, and `get_manifest`. Add it to an MCP host configuration, for example:

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

### JSON contract

The JSON results of all three interfaces are described by JSON Schemas (draft 2020-12) in [`schemas/`](schemas): `manifest`, `info`, `classify`, `convert`, and `error`. `manifest` lists their URLs.

- `schema_version` is the contract's major version, currently `"1"`. New fields, reason codes, and error codes can be added within a major version, so consumers should ignore fields they do not know. Removing a field or changing its meaning raises the major version.
- Every `classify` and `convert` file result carries the source file's `size` (bytes) and `sha256`, or `null` for both if the file could not be read. In `convert`, the hash is computed from the same opened file the converter read.

### Allowed folders

The CLI, API, and MCP server only accept input paths and `output_dir` values on local drives. UNC paths (`\\server\share`), WebDAV paths, mapped network drives, device paths (`\\?\`, `\\.\`), and junctions or symlinks that lead outside the allowed folders are refused with `path_not_allowed` (HTTP 403) before Windows touches the network. This keeps a malicious document or prompt from making an agent send your Windows credentials (NTLM hash) to a remote server.

To narrow access further, for example when an AI agent uses the MCP server, add the folders under **Settings → Security → Allowed folders** (saved as `allowed_folders` in `config.json`), or list them in `MARKITDOWNTOOL_ALLOWED_ROOTS`, separated by `;`:

```powershell
$env:MARKITDOWNTOOL_ALLOWED_ROOTS = "D:\Docs;D:\Results"
```

The environment variable wins over the Settings list; with neither, every local drive is allowed. `info` (and the MCP tool `get_tool_info`) shows the folders in effect as `allowed_roots` and where they came from as `allowed_roots_source` (`environment`, `config`, or `local_drives`). Restart running API or MCP servers after changing either. A trusted file share can be allowed by listing it explicitly.

Links are checked without being followed, so a junction or symlink to a network share is refused before Windows opens it. Recursive folder scans do not enter linked folders. Each file is opened once and the opened handle is checked again, so a folder swapped for a junction after the check cannot redirect the conversion.

### Limits

Integration requests are limited so one caller cannot scan a whole drive or block the others. Exceeding a limit returns `limit_exceeded` (HTTP 413) with the limit name in `details`; a batch that cannot start because another one is still running returns `busy` (HTTP 503). The values are shown by `info` and can be changed in `config.json` (in `%LOCALAPPDATA%\MarkitdownTool`):

```json
"integration_limits": {
    "max_files": 2000,
    "max_scan_depth": 16,
    "max_content_bytes": 52428800,
    "batch_wait_seconds": 300
}
```

- `max_files`: files in one request, after folders are expanded.
- `max_scan_depth`: subfolder levels a recursive scan may enter.
- `max_content_bytes`: total Markdown embedded with `include_content`. Files beyond it still convert but get `"markdown": null` and `"content_omitted": "limit_exceeded"`; read them from `outputs`.
- `batch_wait_seconds`: how long a request waits for a running batch.

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
