import json
import logging
import secrets
import sys
from datetime import datetime
from pathlib import Path

from app_paths import AppPaths, build_app_paths, copy_data_preserving_existing, local_app_data, migrate_legacy_config


APP_VERSION = "2.2.1"
GITHUB_REPOSITORY = "hungnp1999/MarkitdownTool"
IS_FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).parent if IS_FROZEN else Path(__file__).parent
SETTINGS_DIR = local_app_data() / "MarkitdownTool"
CONFIG_FILE = SETTINGS_DIR / "config.json"
API_TOKEN_FILE = SETTINGS_DIR / "api_token"
DEFAULT_OCR_LANGUAGES = ["vie", "eng"]
CLI_EXECUTABLE = "markitdowntool.exe"


def cli_command(*args: str) -> list[str]:
    """The command other tools should run to reach the CLI, for both installed and source builds."""
    if IS_FROZEN:
        return [str(APP_DIR / CLI_EXECUTABLE), *args]
    python = APP_DIR / ".venv" / "Scripts" / "python.exe"
    return [str(python if python.exists() else Path(sys.executable)), str(APP_DIR / "markitdown_cli.py"), *args]


def load_or_create_api_token(token_file: Path | None = None) -> str:
    """Return the local API token, creating a random one on first use."""
    token_file = token_file or API_TOKEN_FILE
    try:
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(token, encoding="utf-8")
    return token


def read_config() -> dict:
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"ocr_languages": list(DEFAULT_OCR_LANGUAGES), "ui_language": "en"}
    if not isinstance(config, dict):
        return {"ocr_languages": list(DEFAULT_OCR_LANGUAGES), "ui_language": "en"}
    languages = config.get("ocr_languages", list(DEFAULT_OCR_LANGUAGES))
    if not isinstance(languages, list):
        languages = []
    config["ocr_languages"] = [code for code in languages if isinstance(code, str) and code]
    if config.get("ui_language") not in {"en", "vi"}:
        config["ui_language"] = "en"
    return config


def save_config(config: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )


def prepare_runtime(log_prefix: str = "process") -> AppPaths:
    """Resolve user data paths, create them, and send logging to a per-session file."""
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    if IS_FROZEN:
        migrate_legacy_config(APP_DIR / "config.json", CONFIG_FILE)

    paths = build_app_paths(APP_DIR, local_app_data(), read_config().get("data_directory"))
    if IS_FROZEN:
        copy_data_preserving_existing(APP_DIR / "tesseract" / "tessdata", paths.tessdata_dir)
    for directory in (paths.output_dir, paths.markdown_dir, paths.ocr_pdf_dir, paths.log_dir, paths.tessdata_dir):
        directory.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        filename=str(paths.log_dir / f"{log_prefix}_{datetime.now():%Y%m%d_%H%M%S}.log"),
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        encoding="utf-8",
    )
    return paths
