"""Protocol-neutral service layer shared by the CLI, HTTP API, and MCP server.

Every function returns plain JSON-serialisable data so each protocol adapter only
handles transport concerns (argument parsing, HTTP, MCP tool schemas).
"""

import logging
import os
import string
import threading
from pathlib import Path
from threading import Event
from typing import Callable, Iterable, Sequence

from app_paths import AppPaths
from app_runtime import APP_DIR, APP_VERSION, GITHUB_REPOSITORY, read_config
from document_core import (
    SUPPORTED_EXTENSIONS,
    FileClassification,
    FileResult,
    ProcessingMode,
    ProgressUpdate,
    classify_file,
    collect_supported_files,
    find_tesseract,
    get_available_languages,
    process_batch,
)


SCHEMA_VERSION = "1"
ALLOWED_ROOTS_ENV = "MARKITDOWNTOOL_ALLOWED_ROOTS"
_ALLOWED_DRIVE_TYPES = {2, 3, 5, 6}  # removable, fixed, CD-ROM, RAM disk; excludes network (4)

_REASON_CODES = {
    "Định dạng này chưa được hỗ trợ.": ("unsupported_format", "This file format is not supported."),
    "PDF cần OCR trước khi tạo Markdown.": ("needs_ocr", "This PDF needs OCR before Markdown conversion."),
    "Tệp không phù hợp với chế độ đã chọn.": ("not_eligible_for_mode", "The file does not apply to the selected mode."),
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


def _with_separator(root: str) -> str:
    return root if root.endswith(os.sep) else root + os.sep


def _local_drive_roots() -> list[str]:
    if os.name != "nt":
        return [os.sep]
    import ctypes

    get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
    return [f"{letter}:\\" for letter in string.ascii_uppercase if get_drive_type(f"{letter}:\\") in _ALLOWED_DRIVE_TYPES]


def allowed_roots(environment: dict[str, str] | None = None) -> list[str]:
    r"""Folders integrations may read from and write to.

    Defaults to every local drive, which excludes UNC shares and mapped network drives.
    MARKITDOWNTOOL_ALLOWED_ROOTS (separated by os.pathsep) replaces the default; an
    administrator can list a trusted share such as \\nas\docs there explicitly.
    """
    configured = (os.environ if environment is None else environment).get(ALLOWED_ROOTS_ENV, "")
    roots = [value.strip() for value in configured.split(os.pathsep) if value.strip()] or _local_drive_roots()
    return [_with_separator(os.path.normcase(os.path.normpath(os.path.abspath(root)))) for root in roots]


def _within(path: str, roots: Sequence[str]) -> bool:
    candidate = os.path.normcase(path)
    return any(candidate == root.rstrip(os.sep) or candidate.startswith(root) for root in roots)


def ensure_allowed_path(value: str | Path, roots: Sequence[str]) -> Path:
    r"""Normalise a caller-supplied path and refuse anything outside the allowed roots.

    The first check runs on the string only, so a UNC path such as \\host\share is
    refused before Windows touches the network (which would send the user's NTLM hash).
    The second check runs after symlinks and junctions are resolved.
    """
    normalized = os.path.normpath(os.path.abspath(os.path.expanduser(str(value))))
    if not _within(normalized, roots):
        raise ServiceError("path_not_allowed", "Path is outside the folders integrations may use.", {"path": str(value)})
    resolved = os.path.realpath(normalized)
    if not _within(resolved, roots):
        raise ServiceError("path_not_allowed", "Path resolves outside the folders integrations may use.", {"path": str(value)})
    return Path(resolved)


def expand_inputs(
    inputs: Iterable[str | Path],
    recursive: bool = False,
    roots: Sequence[str] | None = None,
) -> list[Path]:
    """Turn files and folders into a de-duplicated list of files; folders yield supported files only."""
    roots = allowed_roots() if roots is None else roots
    files: list[Path] = []
    seen: set[str] = set()
    missing: list[str] = []
    for value in inputs:
        path = ensure_allowed_path(value, roots)
        if path.is_dir():
            candidates = collect_supported_files(path, recursive=recursive)
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
    if missing:
        raise ServiceError("not_found", "Some input paths do not exist.", {"paths": missing})
    if not files:
        raise ServiceError("no_input_files", "No files were found in the given paths.")
    return files


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
    }


def file_result_to_dict(result: FileResult, include_content: bool = False) -> dict:
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
        data["markdown"] = markdown.read_text(encoding="utf-8") if markdown else None
    return data


def get_info(paths: AppPaths) -> dict:
    config = read_config()
    return {
        "name": "MarkitdownTool",
        "version": APP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "repository": f"https://github.com/{GITHUB_REPOSITORY}",
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "modes": [mode.value for mode in ProcessingMode],
        "default_ocr_languages": config.get("ocr_languages", []),
        "tesseract": find_tesseract(APP_DIR),
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
    files = expand_inputs(inputs, recursive=recursive, roots=roots)
    return {"files": [classification_to_dict(classify_file(path)) for path in files]}


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
) -> dict:
    roots = allowed_roots() if roots is None else roots
    try:
        mode = ProcessingMode(mode)
    except ValueError as error:
        choices = ", ".join(item.value for item in ProcessingMode)
        raise ServiceError("invalid_mode", f"Unknown mode {mode!r}. Use one of: {choices}.") from error

    files = expand_inputs(inputs, recursive=recursive, roots=roots)
    languages = list(languages) if languages else list(read_config().get("ocr_languages", []))
    classifications = tuple(classify_file(path) for path in files)

    needs_ocr = mode is ProcessingMode.OCR or (
        mode is ProcessingMode.OCR_MARKDOWN and any(item.needs_ocr for item in classifications)
    )
    if needs_ocr:
        _check_ocr_ready(languages, paths)

    if output_dir:
        if not Path(str(output_dir)).expanduser().is_absolute():
            raise ServiceError("invalid_output_dir", "output_dir must be an absolute path.")
        root = ensure_allowed_path(output_dir, roots)
        markdown_dir, ocr_pdf_dir = root / "markdown", root / "ocrpdf"
    else:
        markdown_dir, ocr_pdf_dir = paths.markdown_dir, paths.ocr_pdf_dir

    with _batch_lock:
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
        )

    items = [file_result_to_dict(item, include_content) for item in result.files]
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode.value,
        "languages": languages if needs_ocr else [],
        "cancelled": result.cancelled,
        "summary": {
            "total": len(files),
            "succeeded": sum(item["status"] == "success" for item in items),
            "skipped": sum(item["status"] == "skipped" for item in items),
            "failed": sum(item["status"] == "failed" for item in items),
        },
        "output_dirs": {"markdown": str(markdown_dir), "ocr_pdf": str(ocr_pdf_dir)},
        "files": items,
    }
