"""Protocol-neutral service layer shared by the CLI, HTTP API, and MCP server.

Every function returns plain JSON-serialisable data so each protocol adapter only
handles transport concerns (argument parsing, HTTP, MCP tool schemas).
"""

import logging
import os
import string
import threading
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import BinaryIO, Callable, Iterable, Iterator, Sequence

from app_paths import AppPaths
from app_runtime import APP_DIR, APP_VERSION, GITHUB_REPOSITORY, read_config
from document_core import (
    SUPPORTED_EXTENSIONS,
    FileClassification,
    FileResult,
    ProcessingMode,
    ProgressUpdate,
    ScanLimitExceeded,
    classify_file,
    find_tesseract,
    get_available_languages,
    process_batch,
    scan_supported_files,
)


SCHEMA_VERSION = "1"
ALLOWED_ROOTS_ENV = "MARKITDOWNTOOL_ALLOWED_ROOTS"
_ALLOWED_DRIVE_TYPES = {2, 3, 5, 6}  # removable, fixed, CD-ROM, RAM disk; excludes network (4)
DEFAULT_LIMITS = {
    "max_files": 2000,
    "max_scan_depth": 16,
    "max_content_bytes": 50 * 1024 * 1024,
    "batch_wait_seconds": 300,
}

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


def _local_drive_roots() -> list[str]:
    if os.name != "nt":
        return [os.sep]
    import ctypes

    get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
    return [f"{letter}:\\" for letter in string.ascii_uppercase if get_drive_type(f"{letter}:\\") in _ALLOWED_DRIVE_TYPES]


def allowed_roots(environment: dict[str, str] | None = None) -> list[str]:
    r"""Folders integrations may read from and write to, as canonical paths without a trailing separator.

    Defaults to every local drive, which excludes UNC shares and mapped network drives.
    MARKITDOWNTOOL_ALLOWED_ROOTS (separated by os.pathsep) replaces the default; an
    administrator can list a trusted share such as \\nas\docs there explicitly.
    Roots are resolved with realpath so they use the same letter case as resolved inputs.
    """
    configured = (os.environ if environment is None else environment).get(ALLOWED_ROOTS_ENV, "")
    roots = [value.strip() for value in configured.split(os.pathsep) if value.strip()] or _local_drive_roots()
    return [os.path.realpath(os.path.normpath(os.path.abspath(root))).rstrip(os.sep) for root in roots]


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
    """Turn files and folders into a de-duplicated list of files; folders yield supported files only.

    Folder scans do not enter linked folders and stop at max_scan_depth and max_files.
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
                candidates = scan_supported_files(
                    path,
                    recursive=recursive,
                    max_depth=limits["max_scan_depth"],
                    max_files=max_files - len(files) + 1,
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
        "limits": integration_limits(config),
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
    files = expand_inputs(inputs, recursive=recursive, roots=roots, limits=integration_limits())
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
    limits = integration_limits()
    try:
        mode = ProcessingMode(mode)
    except ValueError as error:
        choices = ", ".join(item.value for item in ProcessingMode)
        raise ServiceError("invalid_mode", f"Unknown mode {mode!r}. Use one of: {choices}.") from error

    files = expand_inputs(inputs, recursive=recursive, roots=roots, limits=limits)
    languages = list(languages) if languages else list(read_config().get("ocr_languages", []))
    classifications = tuple(classify_file(path) for path in files)

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
            open_source=lambda path: open_verified(path, roots),
        )
    finally:
        _batch_lock.release()

    budget = [limits["max_content_bytes"]]
    items = [file_result_to_dict(item, include_content, budget) for item in result.files]
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
