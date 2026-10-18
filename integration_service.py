"""Protocol-neutral service layer shared by the CLI, HTTP API, and MCP server.

Every function returns plain JSON-serialisable data so each protocol adapter only
handles transport concerns (argument parsing, HTTP, MCP tool schemas).
"""

import functools
import hashlib
import importlib
import logging
import os
import string
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import BinaryIO, Callable, Iterable, Iterator, Sequence

from app_paths import AppPaths
from app_runtime import APP_DIR, APP_VERSION, GITHUB_REPOSITORY, cli_command, read_config
from document_core import (
    IMAGE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    TEXT_QUALITY_THRESHOLD,
    FileClassification,
    FileResult,
    ProcessingMode,
    ProgressUpdate,
    ScanLimitExceeded,
    classify_file,
    find_tesseract,
    force_ocr_classification,
    get_available_languages,
    process_batch,
    scan_folder,
)
from office_converter import OFFICE_EXTENSIONS, office_app_available


# Major version of the JSON contract in schemas/. Adding fields or codes keeps it; removing or
# changing the meaning of a field bumps it.
SCHEMA_VERSION = "1"
SCHEMA_BASE_URL = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/schemas"
SCHEMA_NAMES = ("manifest", "info", "classify", "convert", "error")
API_ENDPOINTS = (
    ("GET", "/v1/health"),
    ("GET", "/v1/manifest"),
    ("GET", "/v1/info"),
    ("GET", "/v1/languages"),
    ("POST", "/v1/classify"),
    ("POST", "/v1/convert"),
)
MCP_TOOLS = ("convert_documents", "classify_documents", "list_ocr_languages", "get_tool_info", "get_manifest")
ALLOWED_ROOTS_ENV = "MARKITDOWNTOOL_ALLOWED_ROOTS"
_ALLOWED_DRIVE_TYPES = {2, 3, 5, 6}  # removable, fixed, CD-ROM, RAM disk; excludes network (4)
DEFAULT_LIMITS = {
    "max_files": 2000,
    "max_scan_depth": 16,
    "max_content_bytes": 50 * 1024 * 1024,
    "batch_wait_seconds": 300,
}

# Office can reach the network while it opens a file (ADR-011), so integrations never hand files to it.
_OFFICE_GUI_ONLY = "Định dạng Office cũ chỉ được chuyển đổi trong ứng dụng MarkitdownTool."
INTEGRATION_EXTENSIONS = SUPPORTED_EXTENSIONS - set(OFFICE_EXTENSIONS)
_ALL_INTERFACES = ("gui", "cli", "api", "mcp")
_GUI_ONLY_INTERFACES = ("gui",)

_REASON_CODES = {
    "Định dạng này chưa được hỗ trợ.": ("unsupported_format", "This file format is not supported."),
    "PDF cần OCR trước khi tạo Markdown.": ("needs_ocr", "This PDF needs OCR before Markdown conversion."),
    "Ảnh cần OCR trước khi tạo Markdown.": ("needs_ocr", "This image needs OCR before Markdown conversion."),
    "PDF có chữ bị lỗi mã, cần OCR lại.": ("garbled_text", "This PDF's text layer looks wrongly encoded, so it needs OCR."),
    "Tệp không phù hợp với chế độ đã chọn.": ("not_eligible_for_mode", "The file does not apply to the selected mode."),
    "Cần Microsoft Word để đọc định dạng này.": ("needs_office", "This format needs Microsoft Word, which is not installed."),
    "Cần Microsoft PowerPoint để đọc định dạng này.": ("needs_office", "This format needs Microsoft PowerPoint, which is not installed."),
    "Cần Microsoft Excel để đọc định dạng này.": ("needs_office", "This format needs Microsoft Excel, which is not installed."),
    _OFFICE_GUI_ONLY: (
        "office_gui_only",
        "Legacy Office formats are converted only in the MarkitdownTool app, not through the CLI, API or MCP.",
    ),
}
_STAGE_NAMES = {"Bỏ qua": "skipped", "Lỗi": "error", "Hoàn tất": "done"}

# OCR temporarily rewrites process-wide environment variables, so batches must not overlap.
_batch_lock = threading.Lock()


class ServiceError(Exception):
    """A request the service cannot run, with a stable machine-readable code."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return error


def stage_name(stage: str) -> str:
    """Stable English stage identifier for core stage labels (some are Vietnamese UI text)."""
    return _STAGE_NAMES.get(stage, stage.lower().replace(" + ", "_"))


def _reason(reason: str) -> tuple[str, str]:
    if not reason:
        return "", ""
    return _REASON_CODES.get(reason, ("error", reason))


def _local_drive_roots() -> list[str]:
    if os.name != "nt":
        return [os.sep]
    import ctypes

    get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
    return [f"{letter}:\\" for letter in string.ascii_uppercase if get_drive_type(f"{letter}:\\") in _ALLOWED_DRIVE_TYPES]


def allowed_roots_with_source(
    environment: dict[str, str] | None = None,
    config: dict | None = None,
) -> tuple[list[str], str]:
    r"""Folders integrations may read from and write to, and where that list came from.

    The first non-empty source wins:
    "environment" - MARKITDOWNTOOL_ALLOWED_ROOTS, separated by os.pathsep; an administrator
    can list a trusted share such as \\nas\docs there explicitly;
    "config" - absolute folders in config.json "allowed_folders" (Settings -> Security);
    "local_drives" - every local drive, which excludes UNC shares and mapped network drives.
    Roots are canonical paths without a trailing separator, resolved with realpath so they use
    the same letter case as resolved inputs.
    """
    configured = (os.environ if environment is None else environment).get(ALLOWED_ROOTS_ENV, "")
    roots, source = [value.strip() for value in configured.split(os.pathsep) if value.strip()], "environment"
    if not roots:
        folders = (read_config() if config is None else config).get("allowed_folders")
        if isinstance(folders, list):
            roots = [value.strip() for value in folders if isinstance(value, str) and os.path.isabs(value.strip())]
        source = "config"
    if not roots:
        roots, source = _local_drive_roots(), "local_drives"
    return [os.path.realpath(os.path.normpath(os.path.abspath(root))).rstrip(os.sep) for root in roots], source


def allowed_roots(environment: dict[str, str] | None = None, config: dict | None = None) -> list[str]:
    """The folders from allowed_roots_with_source."""
    return allowed_roots_with_source(environment, config)[0]


def _within(path: str, roots: Sequence[str]) -> bool:
    """Case-insensitive, string-only containment check; never touches the file system."""
    candidate = os.path.normcase(path)
    for root in roots:
        base = os.path.normcase(root)
        if candidate.startswith(base) and candidate[len(base):len(base) + 1] in ("", os.sep):
            return True
    return False


def _is_remote_target(target: str) -> bool:
    r"""True for link targets on a network share (\\host\share, \\?\UNC\...) or a device (\\.\...)."""
    if target.startswith(("\\\\?\\", "\\??\\")):
        return target[4:].upper().startswith("UNC\\")
    return target.startswith(("\\\\", "//"))


def _refuse_remote_links(candidate: str, base: str, value: str | Path) -> None:
    """Check every component below an allowed root without following links.

    os.path.realpath would open each junction or symlink, and Windows contacts the target
    host (sending the user's NTLM hash) when the target is a network share. islink, isjunction
    and readlink only read the link itself.
    """
    current = base
    for part in candidate[len(base):].split(os.sep):
        if not part:
            continue
        current = current + os.sep + part
        if os.path.islink(current) or os.path.isjunction(current):
            try:
                target = os.readlink(current)
            except OSError as error:
                raise ServiceError("path_not_allowed", "Path contains a link that cannot be read.", {"path": str(value)}) from error
            if _is_remote_target(target):
                raise ServiceError("path_not_allowed", "Path contains a link to a network location.", {"path": str(value)})


def ensure_allowed_path(value: str | Path, roots: Sequence[str]) -> Path:
    r"""Normalise a caller-supplied path and refuse anything outside the allowed roots.

    The first check runs on the string only, so a UNC path such as \\host\share is
    refused before Windows touches the network (which would send the user's NTLM hash).
    Links along the path are then read without being followed, so a junction to a share
    is refused before realpath would open it. The last check runs after symlinks and
    junctions are resolved. Both guards are written as a normalised value followed by an
    inline startswith check on the same variable, the form static analysis (CodeQL
    py/path-injection) recognises as a path sanitiser.
    """
    normalized = os.path.normpath(os.path.abspath(os.path.expanduser(str(value))))
    candidate = os.path.normpath(os.path.normcase(normalized))
    for root in roots:
        base = os.path.normcase(root)
        if candidate.startswith(base) and candidate[len(base):len(base) + 1] in ("", os.sep):
            _refuse_remote_links(candidate, base, value)
            break
    else:
        raise ServiceError("path_not_allowed", "Path is outside the folders integrations may use.", {"path": str(value)})
    resolved = os.path.realpath(normalized)
    for root in roots:
        if resolved.startswith(root) and resolved[len(root):len(root) + 1] in ("", os.sep):
            return Path(resolved)
    raise ServiceError("path_not_allowed", "Path resolves outside the folders integrations may use.", {"path": str(value)})


def _final_path(stream: BinaryIO) -> str:
    """Where an open file really is, read from its handle, so a swapped link cannot redirect it."""
    if os.name != "nt":
        return os.path.realpath(stream.name)
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    get_final_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_final_path(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    final = buffer.value
    if final.startswith("\\\\?\\UNC\\"):
        return "\\\\" + final[8:]
    return final[4:] if final.startswith("\\\\?\\") else final


@contextmanager
def open_verified(path: Path, roots: Sequence[str]) -> Iterator[BinaryIO]:
    """Open a checked file and confirm the handle still points inside the allowed roots.

    Closes the gap between ensure_allowed_path and the converter opening the file: a junction
    swapped in between is caught, and the converters read this handle instead of the path.
    """
    with open(path, "rb") as stream:
        if not _within(_final_path(stream), roots):
            raise ServiceError("path_not_allowed", "File moved outside the allowed folders after it was checked.", {"path": str(path)})
        yield stream


def _digest(stream: BinaryIO) -> dict:
    digest = hashlib.file_digest(stream, "sha256")
    size = stream.tell()
    stream.seek(0)
    return {"size": size, "sha256": digest.hexdigest()}


def source_digest(path: Path, roots: Sequence[str]) -> dict:
    """Size and SHA-256 of a checked file, or nulls when it cannot be read."""
    try:
        with open_verified(path, roots) as stream:
            return _digest(stream)
    except (OSError, ServiceError):
        return {"size": None, "sha256": None}


@contextmanager
def _open_and_digest(path: Path, roots: Sequence[str], digests: dict[str, dict]) -> Iterator[BinaryIO]:
    with open_verified(path, roots) as stream:
        digests[str(path)] = _digest(stream)
        yield stream


def integration_limits(config: dict | None = None) -> dict:
    """Resource limits for integration requests: DEFAULT_LIMITS, overridden by config.json "integration_limits"."""
    configured = (read_config() if config is None else config).get("integration_limits")
    limits = dict(DEFAULT_LIMITS)
    if isinstance(configured, dict):
        for key in limits:
            value = configured.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                limits[key] = value
    return limits


def _limit_error(limit: str, value: int) -> ServiceError:
    return ServiceError("limit_exceeded", f"The request exceeds {limit} ({value}).", {"limit": limit, "value": value})


def expand_inputs(
    inputs: Iterable[str | Path],
    recursive: bool = False,
    roots: Sequence[str] | None = None,
    limits: dict | None = None,
) -> list[Path]:
    """Turn files and folders into a de-duplicated list of files.

    Folders yield every file, including unsupported ones, so callers can report them; hidden and
    system files and Office lock files are left out. Folder scans do not enter linked folders
    and stop at max_scan_depth and max_files.
    """
    roots = allowed_roots() if roots is None else roots
    limits = integration_limits() if limits is None else limits
    max_files = limits["max_files"]
    files: list[Path] = []
    seen: set[str] = set()
    missing: list[str] = []
    for value in inputs:
        path = ensure_allowed_path(value, roots)
        if path.is_dir():
            try:
                candidates = scan_folder(
                    path,
                    recursive=recursive,
                    max_depth=limits["max_scan_depth"],
                    max_files=max_files - len(files) + 1,
                    include_unsupported=True,
                )
            except ScanLimitExceeded as error:
                limit = error.limit
                raise _limit_error(limit, limits[limit] if limit == "max_scan_depth" else max_files) from error
        elif path.is_file():
            candidates = [path]
        else:
            missing.append(str(value))
            continue
        for candidate in candidates:
            # Re-check each file: a junction inside an allowed folder can point elsewhere.
            candidate = ensure_allowed_path(candidate, roots)
            key = str(candidate).casefold()
            if key not in seen:
                seen.add(key)
                files.append(candidate)
            if len(files) > max_files:
                raise _limit_error("max_files", max_files)
    if missing:
        raise ServiceError("not_found", "Some input paths do not exist.", {"paths": missing})
    if not files:
        raise ServiceError("no_input_files", "No files were found in the given paths.")
    return files


def classify_for_integration(path: Path) -> FileClassification:
    """classify_file, except that legacy Office formats are refused (office_gui_only)."""
    classification = classify_file(path)
    if path.suffix.lower() not in OFFICE_EXTENSIONS:
        return classification
    return replace(
        classification, supported=False, needs_ocr=False, markdown_eligible=False, ocr_eligible=False, reason=_OFFICE_GUI_ONLY
    )


def classification_to_dict(classification: FileClassification) -> dict:
    reason_code, reason = _reason(classification.reason)
    return {
        "path": str(classification.path),
        "extension": classification.path.suffix.lower(),
        "supported": classification.supported,
        "needs_ocr": classification.needs_ocr,
        "markdown_eligible": classification.markdown_eligible,
        "ocr_eligible": classification.ocr_eligible,
        "reason_code": reason_code,
        "reason": reason,
        "text_quality": classification.text_quality,
        "low_quality_pages": list(classification.low_quality_pages),
    }


def file_result_to_dict(result: FileResult, include_content: bool = False, content_budget: list[int] | None = None) -> dict:
    """JSON form of a result. With include_content, Markdown is embedded while content_budget
    (a one-item list of remaining bytes, shared across a response) allows; later files get
    "markdown": null and "content_omitted": "limit_exceeded"."""
    reason_code, message = _reason(result.message) if result.status.value == "skipped" else ("", result.message)
    data = {
        "source": str(result.path),
        "status": result.status.value,
        "stage": stage_name(result.stage),
        "outputs": [str(path) for path in result.outputs],
        "reason_code": reason_code,
        "message": message,
    }
    if include_content:
        markdown = next((path for path in result.outputs if path.suffix.lower() == ".md"), None)
        data["markdown"] = None
        if markdown:
            size = markdown.stat().st_size
            if content_budget is not None and size > content_budget[0]:
                data["content_omitted"] = "limit_exceeded"
            else:
                data["markdown"] = markdown.read_text(encoding="utf-8")
                if content_budget is not None:
                    content_budget[0] -= size
    return data


def _package_version(name: str) -> str | None:
    from importlib import metadata

    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        try:
            return getattr(importlib.import_module(name), "__version__", None)
        except ImportError:
            return None


@functools.lru_cache(maxsize=4)
def _tesseract_version(executable: str | None) -> str | None:
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first_line = (completed.stdout or completed.stderr).strip().splitlines()[:1]
    return first_line[0].split()[-1].lstrip("v") if first_line else None


def get_manifest(paths: AppPaths) -> dict:
    """What this installation can do, for consumers to check before calling it."""
    config = read_config()
    tesseract = find_tesseract(APP_DIR)
    try:
        languages = sorted(get_available_languages(APP_DIR, paths.data_dir))
    except Exception:
        languages = []
    return {
        "name": "MarkitdownTool",
        "version": APP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "repository": f"https://github.com/{GITHUB_REPOSITORY}",
        "interfaces": {
            "cli": {"command": cli_command(), "json_flag": "--json", "exit_codes": {"0": "ok", "1": "file_failures", "2": "request_error"}},
            "api": {"base_path": "/v1", "auth": "bearer", "default_port": 8765, "endpoints": [f"{method} {path}" for method, path in API_ENDPOINTS]},
            "mcp": {"transport": "stdio", "command": cli_command("mcp"), "tools": list(MCP_TOOLS)},
        },
        "formats": [
            {
                "extension": extension,
                "kind": "image" if extension in IMAGE_EXTENSIONS else "document",
                "markdown": True,
                "ocr": extension == ".pdf" or extension in IMAGE_EXTENSIONS,
                "interfaces": list(_GUI_ONLY_INTERFACES if extension in OFFICE_EXTENSIONS else _ALL_INTERFACES),
            }
            | ({"requires": OFFICE_EXTENSIONS[extension]} if extension in OFFICE_EXTENSIONS else {})
            for extension in sorted(SUPPORTED_EXTENSIONS)
        ],
        "modes": [mode.value for mode in ProcessingMode],
        "ocr": {
            "available": bool(tesseract and languages),
            "engine": "tesseract",
            "languages": languages,
            "default_languages": config.get("ocr_languages", []),
        },
        "engines": {
            "markitdown": _package_version("markitdown"),
            "ocrmypdf": _package_version("ocrmypdf"),
            "pypdfium2": _package_version("pypdfium2"),
            "tesseract": _tesseract_version(tesseract),
            "office": {app: office_app_available(app) for app in sorted(set(OFFICE_EXTENSIONS.values()))},
        },
        "reason_codes": sorted({code for code, _message in _REASON_CODES.values()}),
        "text_quality_threshold": TEXT_QUALITY_THRESHOLD,
        "limits": integration_limits(config),
        "schemas": {name: f"{SCHEMA_BASE_URL}/{name}.schema.json" for name in SCHEMA_NAMES},
    }


def get_info(paths: AppPaths) -> dict:
    config = read_config()
    roots, roots_source = allowed_roots_with_source(config=config)
    return {
        "name": "MarkitdownTool",
        "version": APP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "repository": f"https://github.com/{GITHUB_REPOSITORY}",
        "supported_extensions": sorted(INTEGRATION_EXTENSIONS),
        "modes": [mode.value for mode in ProcessingMode],
        "default_ocr_languages": config.get("ocr_languages", []),
        "tesseract": find_tesseract(APP_DIR),
        "limits": integration_limits(config),
        "allowed_roots": roots,
        "allowed_roots_source": roots_source,
        "data_dir": str(paths.data_dir),
        "markdown_dir": str(paths.markdown_dir),
        "ocr_pdf_dir": str(paths.ocr_pdf_dir),
        "log_dir": str(paths.log_dir),
    }


def list_languages(paths: AppPaths) -> dict:
    from language_manager import list_language_packages

    try:
        packages = list_language_packages(APP_DIR, paths.data_dir)
    except Exception as error:
        raise ServiceError("languages_unavailable", f"Could not read OCR languages: {error}") from error
    return {
        "selected": read_config().get("ocr_languages", []),
        "languages": [
            {"code": package.code, "managed_by_app": package.source == "Ứng dụng", "removable": package.removable}
            for package in packages
        ],
    }


def classify(inputs: Iterable[str | Path], recursive: bool = False, roots: Sequence[str] | None = None) -> dict:
    roots = allowed_roots() if roots is None else roots
    files = expand_inputs(inputs, recursive=recursive, roots=roots, limits=integration_limits())
    return {
        "schema_version": SCHEMA_VERSION,
        "files": [{**classification_to_dict(classify_for_integration(path)), **source_digest(path, roots)} for path in files],
    }


def _check_ocr_ready(languages: list[str], paths: AppPaths) -> None:
    if not find_tesseract(APP_DIR):
        raise ServiceError("tesseract_missing", "Tesseract OCR was not found. Install it or use mode 'markdown'.")
    if not languages:
        raise ServiceError("no_ocr_languages", "No OCR languages were given or configured.")
    try:
        available = set(get_available_languages(APP_DIR, paths.data_dir))
    except Exception as error:
        raise ServiceError("languages_unavailable", f"Could not read OCR languages: {error}") from error
    missing = [code for code in languages if code not in available]
    if missing:
        raise ServiceError("missing_ocr_languages", "Some OCR languages are not installed.", {"languages": missing})


def convert(
    inputs: Iterable[str | Path],
    paths: AppPaths,
    mode: ProcessingMode | str = ProcessingMode.OCR_MARKDOWN,
    languages: list[str] | None = None,
    output_dir: str | Path | None = None,
    recursive: bool = False,
    include_content: bool = False,
    cancel_event: Event | None = None,
    progress_callback: Callable[[ProgressUpdate], None] | None = None,
    roots: Sequence[str] | None = None,
    force_ocr: bool = False,
) -> dict:
    """Process files. force_ocr OCRs every PDF again, replacing its text layer, for PDFs whose
    wrong encoding the text_quality check does not catch."""
    roots = allowed_roots() if roots is None else roots
    limits = integration_limits()
    try:
        mode = ProcessingMode(mode)
    except ValueError as error:
        choices = ", ".join(item.value for item in ProcessingMode)
        raise ServiceError("invalid_mode", f"Unknown mode {mode!r}. Use one of: {choices}.") from error

    files = expand_inputs(inputs, recursive=recursive, roots=roots, limits=limits)
    languages = list(languages) if languages else list(read_config().get("ocr_languages", []))
    classifications = tuple(classify_for_integration(path) for path in files)
    if force_ocr:
        classifications = tuple(force_ocr_classification(item) for item in classifications)
    quality = {str(item.path): item.text_quality for item in classifications}

    needs_ocr = mode is ProcessingMode.OCR or (
        mode is ProcessingMode.OCR_MARKDOWN and any(item.needs_ocr for item in classifications)
    )
    if needs_ocr:
        _check_ocr_ready(languages, paths)

    if output_dir:
        # String-only check: caller data must not reach pathlib before ensure_allowed_path.
        if not os.path.isabs(os.path.expanduser(str(output_dir))):
            raise ServiceError("invalid_output_dir", "output_dir must be an absolute path.")
        root = ensure_allowed_path(output_dir, roots)
        markdown_dir, ocr_pdf_dir = root / "markdown", root / "ocrpdf"
    else:
        markdown_dir, ocr_pdf_dir = paths.markdown_dir, paths.ocr_pdf_dir

    digests: dict[str, dict] = {}
    # One long batch must not block other callers forever.
    if not _batch_lock.acquire(timeout=limits["batch_wait_seconds"]):
        raise ServiceError("busy", "Another batch is still running. Try again later.", {"waited_seconds": limits["batch_wait_seconds"]})
    try:
        logging.getLogger(__name__).info("Integration batch: mode=%s files=%d", mode.value, len(files))
        result = process_batch(
            files,
            mode,
            languages,
            APP_DIR,
            ocr_pdf_dir,
            markdown_dir,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            classifications=classifications,
            data_dir=paths.data_dir,
            open_source=lambda path: _open_and_digest(path, roots, digests),
        )
    finally:
        _batch_lock.release()

    budget = [limits["max_content_bytes"]]
    items = []
    for item in result.files:
        data = file_result_to_dict(item, include_content, budget)
        # Hash what the converter read; files that were skipped were never opened.
        data.update(digests.get(str(item.path)) or source_digest(item.path, roots))
        data["text_quality"] = quality.get(str(item.path))
        items.append(data)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode.value,
        "languages": languages if needs_ocr else [],
        "cancelled": result.cancelled,
        "force_ocr": force_ocr,
        "summary": {
            "total": len(files),
            "succeeded": sum(item["status"] == "success" for item in items),
            "skipped": sum(item["status"] == "skipped" for item in items),
            "failed": sum(item["status"] == "failed" for item in items),
        },
        "output_dirs": {"markdown": str(markdown_dir), "ocr_pdf": str(ocr_pdf_dir)},
        "files": items,
    }
