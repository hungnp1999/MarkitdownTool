import os
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from document_core import find_tesseract


LANGUAGE_CODE_PATTERN = re.compile(r"^[a-z0-9_]+$")
LANGUAGE_DATA_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/refs/heads/main/{code}.traineddata"
MAX_LANGUAGE_DATA_SIZE = 100 * 1024 * 1024


@dataclass(frozen=True)
class LanguagePackage:
    code: str
    source: str
    removable: bool


def managed_tessdata_dir(data_dir: Path) -> Path:
    return data_dir / "tesseract" / "tessdata"


def _list_languages(project_dir: Path, data_dir: Path | None = None) -> tuple[set[str], Path | None]:
    tesseract = find_tesseract(project_dir)
    if not tesseract:
        raise RuntimeError("Không tìm thấy Tesseract OCR.")

    environment = os.environ.copy()
    managed_dir = managed_tessdata_dir(data_dir) if data_dir else None
    if managed_dir and managed_dir.is_dir() and any(managed_dir.glob("*.traineddata")):
        environment["TESSDATA_PREFIX"] = str(managed_dir)

    result = subprocess.run(
        [tesseract, "--list-langs"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Không thể đọc danh sách ngôn ngữ OCR.")

    lines = result.stdout.splitlines()
    tessdata_dir = None
    if lines:
        match = re.search(r'"(.+?)"', lines[0])
        if match:
            candidate = Path(match.group(1))
            if candidate.is_dir():
                tessdata_dir = candidate

    languages = {
        line.strip()
        for line in lines
        if line.strip()
        and "List of available languages" not in line
        and LANGUAGE_CODE_PATTERN.fullmatch(line.strip())
    }
    return languages, tessdata_dir


def list_language_packages(project_dir: Path, data_dir: Path | None = None) -> list[LanguagePackage]:
    available, _ = _list_languages(project_dir, data_dir)
    managed_dir = managed_tessdata_dir(data_dir or project_dir)
    managed = {
        path.name.removesuffix(".traineddata")
        for path in managed_dir.glob("*.traineddata")
    } if managed_dir.is_dir() else set()

    return [
        LanguagePackage(
            code,
            "Ứng dụng" if code in managed else "Tesseract hệ thống",
            code in managed and code != "osd",
        )
        for code in sorted(available | managed)
    ]


def install_language(code: str, project_dir: Path, data_dir: Path | None = None) -> Path:
    if not LANGUAGE_CODE_PATTERN.fullmatch(code):
        raise ValueError("Mã ngôn ngữ không hợp lệ.")

    target_dir = managed_tessdata_dir(data_dir or project_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{code}.traineddata"
    temporary = target.with_suffix(".traineddata.download")
    request = urllib.request.Request(  # noqa: S310 - fixed https URL
        LANGUAGE_DATA_URL.format(code=code),
        headers={"User-Agent": "MarkitdownTool"},
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https URL
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_LANGUAGE_DATA_SIZE:
                raise ValueError("Gói ngôn ngữ vượt quá giới hạn kích thước 100 MB.")

            total = 0
            with temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_LANGUAGE_DATA_SIZE:
                        raise ValueError("Gói ngôn ngữ vượt quá giới hạn kích thước 100 MB.")
                    output.write(chunk)

        if total < 1024:
            raise ValueError("Tệp tải về quá nhỏ, có thể không phải gói ngôn ngữ hợp lệ.")

        os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def uninstall_language(code: str, project_dir: Path, data_dir: Path | None = None) -> bool:
    if not LANGUAGE_CODE_PATTERN.fullmatch(code):
        raise ValueError("Mã ngôn ngữ không hợp lệ.")
    if code == "osd":
        raise ValueError("Không thể gỡ gói osd vì OCR dùng gói này để nhận diện hướng trang.")

    target = managed_tessdata_dir(data_dir or project_dir) / f"{code}.traineddata"
    if not target.is_file():
        return False
    target.unlink()
    return True


def prepare_managed_language_data(
    project_dir: Path,
    languages: list[str],
    data_dir: Path | None = None,
) -> Path | None:
    managed_dir = managed_tessdata_dir(data_dir or project_dir)
    if not managed_dir.is_dir():
        return None
    if any(managed_dir.glob("*.traineddata")):
        return managed_dir

    _, system_tessdata_dir = _list_languages(project_dir)
    if not system_tessdata_dir or system_tessdata_dir.resolve() == managed_dir.resolve():
        return managed_dir

    for code in set(languages) | {"osd"}:
        managed_file = managed_dir / f"{code}.traineddata"
        system_file = system_tessdata_dir / f"{code}.traineddata"
        if not managed_file.exists() and system_file.is_file():
            shutil.copy2(system_file, managed_file)

    return managed_dir