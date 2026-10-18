import functools
import os


# Legacy and OpenDocument formats that MarkItDown cannot read; the matching Office app saves them as OOXML first.
OFFICE_EXTENSIONS: dict[str, str] = {
    ".doc": "Word",
    ".rtf": "Word",
    ".odt": "Word",
    ".ppt": "PowerPoint",
    ".odp": "PowerPoint",
    ".ods": "Excel",
}


@functools.lru_cache(maxsize=None)
def office_app_available(app: str) -> bool:
    """True when the Office app is registered for COM automation (HKCR\\<app>.Application\\CLSID)."""
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{app}.Application\CLSID"):
            return True
    except OSError:
        return False
