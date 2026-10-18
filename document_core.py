import hashlib
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from threading import Event
from typing import BinaryIO, Callable, ContextManager, Iterable, Iterator

from office_converter import OFFICE_EXTENSIONS, convert_with_timeout, office_app_available


DOCUMENT_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".csv",
    ".html", ".htm", ".epub", ".txt", ".md", ".json", ".xml",
}
# Images have no text layer, so they always go through OCR (image -> searchable PDF -> Markdown).
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
SUPPORTED_EXTENSIONS = DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS | set(OFFICE_EXTENSIONS)
# Used when an image has no usable resolution metadata; OCRmyPDF refuses such images otherwise.
DEFAULT_IMAGE_DPI = 300
# Pages whose share of correctly encoded words is below this are OCRed again (force OCR).
TEXT_QUALITY_THRESHOLD = 0.8
# Pages with fewer words that contain letters are not judged.
MIN_JUDGED_WORDS = 3
# Latin-1 symbols that legitimately touch letters or numbers (25°C, ±0.5, µm, ABC®).
_COMMON_SYMBOLS = frozenset("°±©®«»§²³¼½¾·µ")


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
    text_quality: float | None = None
    # OCR must replace the existing text layer, on ocr_pages (1-based) or on every page when empty.
    force_ocr: bool = False
    ocr_pages: tuple[int, ...] = ()
    low_quality_pages: tuple[int, ...] = ()


@dataclass(frozen=True)
class PdfTextReport:
    readable: bool
    empty_pages: tuple[int, ...] = ()
    low_quality_pages: tuple[int, ...] = ()
    text_quality: float | None = None


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


class ScanLimitExceeded(Exception):
    def __init__(self, limit: str, value: int):
        super().__init__(f"Folder scan exceeded {limit}={value}.")
        self.limit = limit
        self.value = value


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


def _is_broken_char(char: str) -> bool:
    return char in "�ÿ" or "" <= char <= "" or "\x80" <= char <= "\x9f"


def _script(char: str) -> str:
    return unicodedata.name(char, "").split(" ", 1)[0]


def is_garbled_word(word: str) -> bool:
    """Signs that a word was extracted with the wrong encoding, independent of its language:

    - replacement, private-use or C1 control characters, or U+00FF, which broken encodings
      produce for every character they cannot map ("hi�u", "hiÿu");
    - a Latin-1 symbol (U+00A1-U+00BF) between two letters, as TCVN3 fonts produce ("Ngµy"),
      or an uncommon one anywhere in a word with letters ("®¹t");
    - an accented Latin capital right after a lowercase letter ("hiÖu", "chuÈn"), which TCVN3
      produces for lowercase toned vowels;
    - Latin and Cyrillic letters in one word, or a combining mark that follows no letter,
      as fonts without a usable Unicode map produce ("3+Ҫ1", "6͙").
    """
    has_letters = any(char.isalpha() for char in word)
    scripts = set()
    for index, char in enumerate(word):
        previous = word[index - 1] if index else ""
        following = word[index + 1] if index + 1 < len(word) else ""
        if _is_broken_char(char):
            return True
        if "¡" <= char <= "¿":
            if char not in _COMMON_SYMBOLS and has_letters:
                return True
            if previous.isalpha() and following.isalpha():
                return True
        elif char.isupper() and not char.isascii() and previous.islower() and _script(char) == "LATIN":
            return True
        elif unicodedata.category(char) == "Mn" and not (previous.isalpha() or unicodedata.category(previous or " ") == "Mn"):
            return True
        if char.isalpha():
            scripts.add(_script(char))
    return {"LATIN", "CYRILLIC"} <= scripts


def _words(text: str) -> list[str]:
    # Some PDFs separate words with C0 control characters (often U+0003) instead of spaces.
    separated = "".join(" " if unicodedata.category(char) == "Cc" else char for char in text)
    return [word for word in separated.split() if any(char.isalpha() or _is_broken_char(char) for char in word)]


def text_quality(text: str) -> tuple[float | None, int]:
    """Share of words that look correctly encoded, and how many words were judged.

    The score is None when fewer than MIN_JUDGED_WORDS words contain letters.
    """
    words = _words(text)
    if len(words) < MIN_JUDGED_WORDS:
        return None, len(words)
    return 1 - sum(is_garbled_word(word) for word in words) / len(words), len(words)


def analyze_pdf_text(pdf_path: Path) -> PdfTextReport:
    """Find pages without text and pages whose text layer looks wrongly encoded."""
    try:
        import pypdfium2 as pdfium

        empty, low_quality = [], []
        weighted, judged = 0.0, 0
        with pdfium.PdfDocument(str(pdf_path)) as document:
            for page_index in range(len(document)):
                page = document[page_index]
                try:
                    text_page = page.get_textpage()
                    try:
                        text = text_page.get_text_bounded()
                    finally:
                        text_page.close()
                finally:
                    page.close()
                if not text.strip():
                    empty.append(page_index + 1)
                    continue
                quality, words = text_quality(text)
                if quality is None:
                    continue
                weighted += quality * words
                judged += words
                if quality < TEXT_QUALITY_THRESHOLD:
                    low_quality.append(page_index + 1)
        return PdfTextReport(True, tuple(empty), tuple(low_quality), round(weighted / judged, 3) if judged else None)
    except Exception:
        return PdfTextReport(False)


def pdf_needs_ocr(pdf_path: Path) -> bool:
    report = analyze_pdf_text(pdf_path)
    return not report.readable or bool(report.empty_pages or report.low_quality_pages)


def classify_file(file_path: str | Path) -> FileClassification:
    path = Path(file_path)
    extension = path.suffix.lower()
    supported = extension in SUPPORTED_EXTENSIONS
    is_image = extension in IMAGE_EXTENSIONS
    report = analyze_pdf_text(path) if extension == ".pdf" else PdfTextReport(True)
    garbled = bool(report.low_quality_pages)
    needs_ocr = is_image or not report.readable or bool(report.empty_pages) or garbled
    office_app = OFFICE_EXTENSIONS.get(extension)
    office_missing = bool(office_app) and not office_app_available(office_app)

    if not supported:
        reason = "Định dạng này chưa được hỗ trợ."
    elif office_missing:
        reason = f"Cần Microsoft {office_app} để đọc định dạng này."
    elif is_image:
        reason = "Ảnh cần OCR trước khi tạo Markdown."
    elif garbled:
        reason = "PDF có chữ bị lỗi mã, cần OCR lại."
    elif needs_ocr:
        reason = "PDF cần OCR trước khi tạo Markdown."
    else:
        reason = ""

    return FileClassification(
        path=path,
        supported=supported,
        needs_ocr=needs_ocr,
        markdown_eligible=supported and not needs_ocr and not office_missing,
        ocr_eligible=needs_ocr,
        reason=reason,
        text_quality=report.text_quality,
        force_ocr=garbled,
        ocr_pages=tuple(sorted(report.empty_pages + report.low_quality_pages)) if garbled else (),
        low_quality_pages=report.low_quality_pages,
    )


def force_ocr_classification(classification: FileClassification) -> FileClassification:
    """Treat a PDF as needing OCR on every page, replacing its text layer (--force-ocr)."""
    if classification.path.suffix.lower() != ".pdf" or not classification.supported:
        return classification
    return replace(
        classification,
        needs_ocr=True,
        markdown_eligible=False,
        ocr_eligible=True,
        force_ocr=True,
        ocr_pages=(),
        reason=classification.reason or "PDF cần OCR trước khi tạo Markdown.",
    )


def classify_files(file_paths: Iterable[str | Path]) -> tuple[FileClassification, ...]:
    return tuple(classify_file(path) for path in file_paths)


def _is_document_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in SUPPORTED_EXTENSIONS and not name.startswith("~$")


def collect_supported_files(folder: str | Path, recursive: bool = False) -> list[Path]:
    root = Path(folder)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a folder: {root}")
    candidates = root.rglob("*") if recursive else root.iterdir()
    return sorted(
        (path for path in candidates if path.is_file() and _is_document_name(path.name)),
        key=lambda path: str(path).casefold(),
    )


def _is_hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith((".", "~$")):
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))


def scan_folder(
    folder: str | Path,
    recursive: bool = False,
    max_depth: int | None = None,
    max_files: int | None = None,
    include_unsupported: bool = False,
) -> list[Path]:
    """Like collect_supported_files, but never follows junctions or symlinks and stops at the limits.

    A linked folder is not entered, because its target may be a network share. A linked file is
    returned without being opened, so the caller can check where it points first.
    include_unsupported also returns files of other types, so callers can report them instead of
    dropping them; hidden and system files, Office lock files (~$), and anything inside hidden
    folders are still left out unless they are supported documents.
    """
    root = Path(folder)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a folder: {root}")
    found: list[Path] = []

    def wanted(entry: os.DirEntry, hidden_folder: bool) -> bool:
        if _is_document_name(entry.name):
            return True
        return include_unsupported and not hidden_folder and not _is_hidden(entry)

    def walk(directory: Path, depth: int, hidden_folder: bool) -> Iterator[Path]:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink() or entry.is_junction():
                    if not entry.is_junction() and wanted(entry, hidden_folder):
                        yield Path(entry.path)
                elif entry.is_dir(follow_symlinks=False):
                    if recursive:
                        if max_depth is not None and depth >= max_depth:
                            raise ScanLimitExceeded("max_scan_depth", max_depth)
                        yield from walk(Path(entry.path), depth + 1, hidden_folder or _is_hidden(entry))
                elif entry.is_file(follow_symlinks=False) and wanted(entry, hidden_folder):
                    yield Path(entry.path)

    for path in walk(root, 0, False):
        if max_files is not None and len(found) >= max_files:
            raise ScanLimitExceeded("max_files", max_files)
        found.append(path)
    return sorted(found, key=lambda path: str(path).casefold())


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


def _has_alpha(image) -> bool:
    return image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info


def _flatten(frame):
    from PIL import Image

    rgba = frame.convert("RGBA")
    background = Image.new("RGB", rgba.size, "white")
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def prepare_image_input(source: Path, stream: BinaryIO | None, work_dir: Path) -> tuple[str | BinaryIO, dict]:
    """Make an image acceptable to OCRmyPDF, which refuses images without a resolution and
    images with an alpha channel. Returns the input to pass on and extra OCRmyPDF options."""
    from PIL import Image, ImageSequence

    with Image.open(stream or source) as image:
        dpi = image.info.get("dpi")
        usable_dpi = bool(dpi) and min(dpi) >= 50
        options = {} if usable_dpi else {"image_dpi": DEFAULT_IMAGE_DPI}
        if not _has_alpha(image):
            if stream:
                stream.seek(0)
            return stream or str(source), options
        frames = [_flatten(frame) for frame in ImageSequence.Iterator(image)]

    flattened = work_dir / "flattened.tiff"
    frames[0].save(
        flattened,
        save_all=True,
        append_images=frames[1:],
        compression="tiff_deflate",
        dpi=dpi if usable_dpi else (DEFAULT_IMAGE_DPI, DEFAULT_IMAGE_DPI),
    )
    return str(flattened), options


def run_ocr(
    source: Path,
    languages: list[str],
    output_dir: Path,
    project_dir: Path,
    data_dir: Path | None = None,
    stream: BinaryIO | None = None,
    force_pages: tuple[int, ...] | None = None,
) -> Path:
    """OCR into a searchable PDF. Pages that already have text are kept, unless force_pages is
    given: then the text layer is replaced on those pages, or on every page when it is empty."""
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
    is_image = source.suffix.lower() in IMAGE_EXTENSIONS
    os.environ.update(environment)
    try:
        with tempfile.TemporaryDirectory(prefix="markitdowntool_image_") if is_image else nullcontext() as work_dir:
            input_file, image_options = (
                prepare_image_input(source, stream, Path(work_dir)) if is_image else (stream or str(source), {})
            )
            if force_pages is None:
                text_options = {"skip_text": True}
            else:
                text_options = {"force_ocr": True}
                if force_pages:
                    text_options["pages"] = ",".join(str(page) for page in force_pages)
            with hide_ocr_console_windows():
                exit_code = ocrmypdf.ocr(
                    input_file,
                    str(output_pdf),
                    language=languages,
                    deskew=True,
                    rotate_pages=True,
                    progress_bar=False,
                    use_threads=os.name == "nt",
                    **text_options,
                    **image_options,
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


def convert_markdown(
    source: Path,
    output_dir: Path,
    output_source: Path | None = None,
    stream: BinaryIO | None = None,
) -> Path:
    from markitdown import MarkItDown, StreamInfo

    logging.getLogger(__name__).info("Markdown conversion started: %s", source.name)
    if stream is None:
        result = MarkItDown().convert(str(source))
    else:
        info = StreamInfo(extension=source.suffix.lower(), filename=source.name)
        result = MarkItDown().convert_stream(stream, stream_info=info)
    output_markdown = output_path_for(output_source or source, output_dir, ".md")
    output_markdown.write_text(result.text_content, encoding="utf-8")
    logging.getLogger(__name__).info("Markdown conversion completed: %s", output_markdown.name)
    return output_markdown


def convert_office_markdown(source: Path, output_dir: Path, stream: BinaryIO | None = None) -> Path:
    """Convert a legacy Office file to OOXML in a private temporary folder, then to Markdown."""
    with tempfile.TemporaryDirectory(prefix="markitdowntool-office-", ignore_cleanup_errors=True) as work:
        work_dir = Path(work)
        copy = work_dir / f"input{source.suffix.lower()}"
        with copy.open("wb") as target:
            if stream is None:
                with source.open("rb") as original:
                    shutil.copyfileobj(original, target)
            else:
                shutil.copyfileobj(stream, target)
        converted = convert_with_timeout(copy, work_dir)
        return convert_markdown(converted, output_dir, output_source=source)


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
    open_source: Callable[[Path], ContextManager[BinaryIO]] | None = None,
) -> BatchResult:
    """Process files in order. open_source, when given, opens each source file (for example to
    verify where the opened handle really points); OCR and Markdown then read that stream
    instead of reopening the path."""
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

        force_pages = classification.ocr_pages if classification.force_ocr else None
        try:
            outputs: list[Path] = []
            with open_source(path) if open_source else nullcontext() as stream:
                if mode is ProcessingMode.OCR:
                    if progress_callback:
                        progress_callback(ProgressUpdate(index, len(paths), path, "OCR"))
                    outputs.append(run_ocr(path, languages, ocr_output_dir, project_dir, data_dir, stream, force_pages))
                    stage = "OCR"
                else:
                    markdown_source = path
                    if mode is ProcessingMode.OCR_MARKDOWN and classification.needs_ocr:
                        if progress_callback:
                            progress_callback(ProgressUpdate(index, len(paths), path, "OCR"))
                        ocr_pdf = run_ocr(path, languages, ocr_output_dir, project_dir, data_dir, stream, force_pages)
                        outputs.append(ocr_pdf)
                        markdown_source = ocr_pdf
                    if progress_callback:
                        progress_callback(ProgressUpdate(index, len(paths), path, "Markdown"))
                    if path.suffix.lower() in OFFICE_EXTENSIONS:
                        outputs.append(convert_office_markdown(path, markdown_output_dir, stream))
                    else:
                        outputs.append(
                            convert_markdown(
                                markdown_source,
                                markdown_output_dir,
                                output_source=path if markdown_source != path else None,
                                stream=stream if markdown_source == path else None,
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