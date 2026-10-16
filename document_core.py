import hashlib
import logging
import os
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Event
from typing import Callable, Iterable


SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx"}


class ProcessingMode(str, Enum):
    OCR = "ocr"
    MARKDOWN = "markdown"
    OCR_MARKDOWN = "ocr_markdown"


class FileStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class FileClassification:
    path: Path
    supported: bool
    needs_ocr: bool
    markdown_eligible: bool
    ocr_eligible: bool
    reason: str = ""


@dataclass(frozen=True)
class FileResult:
    path: Path
    status: FileStatus
    stage: str
    outputs: tuple[Path, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class ProgressUpdate:
    completed: int
    total: int
    path: Path
    stage: str


@dataclass(frozen=True)
class BatchResult:
    files: tuple[FileResult, ...]
    cancelled: bool = False


def find_tesseract(project_dir: Path) -> str | None:
    bundled_exe = project_dir / "tesseract" / "tesseract.exe"
    if bundled_exe.exists():
        return str(bundled_exe)

    executable = shutil.which("tesseract")
    if executable:
        return executable

    if os.name == "nt":
        for candidate in (
            Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
            Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        ):
            if candidate.exists():
                return str(candidate)

    return None


def get_available_languages(project_dir: Path, data_dir: Path | None = None) -> list[str]:
    from language_manager import list_language_packages

    return [package.code for package in list_language_packages(project_dir, data_dir)]


def pdf_needs_ocr(pdf_path: Path) -> bool:
    try:
        import pypdfium2 as pdfium

        with pdfium.PdfDocument(str(pdf_path)) as document:
            for page_index in range(len(document)):
                page = document[page_index]
                try:
                    text_page = page.get_textpage()
                    try:
                        if not text_page.get_text_bounded().strip():
                            return True
                    finally:
                        text_page.close()
                finally:
                    page.close()
        return False
    except Exception:
        return True


def classify_file(file_path: str | Path) -> FileClassification:
    path = Path(file_path)
    extension = path.suffix.lower()
    supported = extension in SUPPORTED_EXTENSIONS
    needs_ocr = extension == ".pdf" and pdf_needs_ocr(path)

    if not supported:
        reason = "Định dạng này chưa được hỗ trợ."
    elif needs_ocr:
        reason = "PDF cần OCR trước khi tạo Markdown."
    else:
        reason = ""

    return FileClassification(
        path=path,
        supported=supported,
        needs_ocr=needs_ocr,
        markdown_eligible=supported and not needs_ocr,
        ocr_eligible=extension == ".pdf" and needs_ocr,
        reason=reason,
    )


def classify_files(file_paths: Iterable[str | Path]) -> tuple[FileClassification, ...]:
    return tuple(classify_file(path) for path in file_paths)


def collect_supported_files(folder: str | Path, recursive: bool = False) -> list[Path]:
    root = Path(folder)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a folder: {root}")
    candidates = root.rglob("*") if recursive else root.iterdir()
    return sorted(
        (
            path
            for path in candidates
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS and not path.name.startswith("~$")
        ),
        key=lambda path: str(path).casefold(),
    )


def has_subfolders(folder: str | Path) -> bool:
    return any(path.is_dir() for path in Path(folder).iterdir())


def output_path_for(source: Path, output_dir: Path, extension: str) -> Path:
    identity = os.path.normcase(str(source.resolve()))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    return output_dir / f"{source.stem}_{digest}{extension}"


@contextmanager
def hide_ocr_console_windows():
    if os.name != "nt":
        yield
        return

    from ocrmypdf.subprocess import _run as ocrmypdf_subprocess

    original_run = ocrmypdf_subprocess.subprocess_run
    original_popen = ocrmypdf_subprocess.Popen
    creation_flag = subprocess.CREATE_NO_WINDOW

    def run_without_console(*args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | creation_flag
        return original_run(*args, **kwargs)

    def popen_without_console(*args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | creation_flag
        return original_popen(*args, **kwargs)

    ocrmypdf_subprocess.subprocess_run = run_without_console
    ocrmypdf_subprocess.Popen = popen_without_console
    try:
        yield
    finally:
        ocrmypdf_subprocess.subprocess_run = original_run
        ocrmypdf_subprocess.Popen = original_popen


def run_ocr(
    source: Path,
    languages: list[str],
    output_dir: Path,
    project_dir: Path,
    data_dir: Path | None = None,
) -> Path:
    import ocrmypdf

    output_pdf = output_path_for(source, output_dir, ".pdf")
    logging.getLogger(__name__).info("OCR started: %s", source.name)
    tesseract = find_tesseract(project_dir)
    environment = os.environ.copy()
    if tesseract:
        tesseract_dir = str(Path(tesseract).parent)
        environment["PATH"] = tesseract_dir + os.pathsep + environment.get("PATH", "")
    from language_manager import prepare_managed_language_data

    tessdata_dir = prepare_managed_language_data(project_dir, languages, data_dir)
    if tessdata_dir:
        environment["TESSDATA_PREFIX"] = str(tessdata_dir)

    previous_environment = {
        key: os.environ.get(key)
        for key in ("PATH", "TESSDATA_PREFIX")
    }
    os.environ.update(environment)
    try:
        with hide_ocr_console_windows():
            exit_code = ocrmypdf.ocr(
                str(source),
                str(output_pdf),
                language=languages,
                deskew=True,
                rotate_pages=True,
                skip_text=True,
                progress_bar=False,
                use_threads=os.name == "nt",
            )
    finally:
        for key, value in previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    if exit_code != ocrmypdf.ExitCode.ok:
        raise RuntimeError(f"OCRmyPDF failed with exit code {exit_code}.")
    logging.getLogger(__name__).info("OCR completed: %s", output_pdf.name)
    return output_pdf


def convert_markdown(source: Path, output_dir: Path, output_source: Path | None = None) -> Path:
    from markitdown import MarkItDown

    logging.getLogger(__name__).info("Markdown conversion started: %s", source.name)
    result = MarkItDown().convert(str(source))
    output_markdown = output_path_for(output_source or source, output_dir, ".md")
    output_markdown.write_text(result.text_content, encoding="utf-8")
    logging.getLogger(__name__).info("Markdown conversion completed: %s", output_markdown.name)
    return output_markdown


def _is_eligible(classification: FileClassification, mode: ProcessingMode) -> bool:
    if mode is ProcessingMode.OCR:
        return classification.ocr_eligible
    if mode is ProcessingMode.MARKDOWN:
        return classification.markdown_eligible
    return classification.supported


def process_batch(
    file_paths: Iterable[str | Path],
    mode: ProcessingMode,
    languages: list[str],
    project_dir: Path,
    ocr_output_dir: Path,
    markdown_output_dir: Path,
    cancel_event: Event | None = None,
    progress_callback: Callable[[ProgressUpdate], None] | None = None,
    classifications: Iterable[FileClassification] | None = None,
    data_dir: Path | None = None,
) -> BatchResult:
    paths = tuple(Path(path) for path in file_paths)
    classified = tuple(classifications) if classifications is not None else classify_files(paths)
    if len(classified) != len(paths):
        raise ValueError("Số kết quả phân loại không khớp với danh sách tệp.")
    if mode in (ProcessingMode.OCR, ProcessingMode.OCR_MARKDOWN):
        ocr_output_dir.mkdir(parents=True, exist_ok=True)
    if mode in (ProcessingMode.MARKDOWN, ProcessingMode.OCR_MARKDOWN):
        markdown_output_dir.mkdir(parents=True, exist_ok=True)

    results: list[FileResult] = []
    cancelled = False
    logger = logging.getLogger(__name__)
    logger.info("Batch started: mode=%s files=%d", mode.value, len(paths))

    for index, classification in enumerate(classified, start=1):
        if cancel_event and cancel_event.is_set():
            cancelled = True
            break

        path = classification.path
        if not _is_eligible(classification, mode):
            reason = classification.reason or "Tệp không phù hợp với chế độ đã chọn."
            logger.info("File skipped: %s; reason=%s", path.name, reason)
            results.append(FileResult(path, FileStatus.SKIPPED, "Bỏ qua", message=reason))
            if progress_callback:
                progress_callback(ProgressUpdate(index, len(paths), path, "Bỏ qua"))
            continue

        try:
            outputs: list[Path] = []
            if mode is ProcessingMode.OCR:
                if progress_callback:
                    progress_callback(ProgressUpdate(index, len(paths), path, "OCR"))
                outputs.append(run_ocr(path, languages, ocr_output_dir, project_dir, data_dir))
                stage = "OCR"
            else:
                markdown_source = path
                if mode is ProcessingMode.OCR_MARKDOWN and classification.needs_ocr:
                    if progress_callback:
                        progress_callback(ProgressUpdate(index, len(paths), path, "OCR"))
                    ocr_pdf = run_ocr(path, languages, ocr_output_dir, project_dir, data_dir)
                    outputs.append(ocr_pdf)
                    markdown_source = ocr_pdf
                if progress_callback:
                    progress_callback(ProgressUpdate(index, len(paths), path, "Markdown"))
                outputs.append(
                    convert_markdown(
                        markdown_source,
                        markdown_output_dir,
                        output_source=path if markdown_source != path else None,
                    )
                )
                stage = "OCR + Markdown" if classification.needs_ocr and mode is ProcessingMode.OCR_MARKDOWN else "Markdown"

            results.append(FileResult(path, FileStatus.SUCCESS, stage, tuple(outputs)))
            logger.info(
                "File completed: %s; stage=%s; outputs=%s",
                path.name,
                stage,
                ", ".join(output.name for output in outputs),
            )
        except Exception as error:
            logger.exception("Processing failed for %s", path)
            results.append(FileResult(path, FileStatus.FAILED, "Lỗi", message=str(error)))

        if progress_callback:
            progress_callback(ProgressUpdate(index, len(paths), path, "Hoàn tất"))

    if cancel_event and cancel_event.is_set() and len(results) < len(paths):
        cancelled = True

    successful = sum(result.status is FileStatus.SUCCESS for result in results)
    skipped = sum(result.status is FileStatus.SKIPPED for result in results)
    failed = sum(result.status is FileStatus.FAILED for result in results)
    logger.info(
        "Batch completed: mode=%s succeeded=%d skipped=%d failed=%d cancelled=%s",
        mode.value,
        successful,
        skipped,
        failed,
        cancelled,
    )
    return BatchResult(tuple(results), cancelled)