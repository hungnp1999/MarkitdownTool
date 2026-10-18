import functools
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable


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


# app -> (SaveAs file format, extension): wdFormatXMLDocument, ppSaveAsOpenXMLPresentation, xlOpenXMLWorkbook.
_SAVE_FORMATS = {"Word": (12, ".docx"), "PowerPoint": (24, ".pptx"), "Excel": (51, ".xlsx")}
_EXECUTABLES = {"Word": "WINWORD.EXE", "PowerPoint": "POWERPNT.EXE", "Excel": "EXCEL.EXE"}
_FORCE_DISABLE_MACROS = 3  # msoAutomationSecurityForceDisable
# A wrong password makes COM raise instead of showing the password dialog.
_DUMMY_PASSWORD = "x"
_QUIT_GRACE_SECONDS = 3


def _system_tool(name: str) -> str:
    # Full path, so a same-named program earlier on PATH cannot run instead.
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", name)


def _office_pids(app: str) -> set[int]:
    completed = subprocess.run(
        [_system_tool("tasklist.exe"), "/FI", f"IMAGENAME eq {_EXECUTABLES[app]}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    pids = set()
    for line in completed.stdout.splitlines():
        fields = [field.strip('"') for field in line.split('","')]
        if len(fields) > 1 and fields[1].isdigit():
            pids.add(int(fields[1]))
    return pids


def kill_process(pid: int) -> bool:
    """Kill one process tree by PID; never by image name, which would close the user's own Office windows."""
    completed = subprocess.run(
        [_system_tool("taskkill.exe"), "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return completed.returncode == 0


def find_instance_pid(app: str, instance, before: set[int]) -> int | None:
    """PID of the instance just started: from its window when Office exposes one, else the one new process."""
    try:
        import win32gui
        import win32process

        hwnd = 0
        if app == "Word":
            marker = f"markitdowntool-{uuid.uuid4()}"
            instance.Caption = marker
            hwnd = win32gui.FindWindow("OpusApp", marker)
        elif app == "Excel":
            hwnd = instance.Hwnd
        if hwnd:
            return win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception:  # noqa: BLE001 - fall back to the process list
        pass
    # PowerPoint has no usable window handle when hidden; two new processes mean another start raced this one.
    new = _office_pids(app) - before
    return new.pop() if len(new) == 1 else None


def start_office(app: str):
    """Start a private Office instance with macros force-disabled and no dialogs. Returns (instance, pid or None)."""
    import win32com.client

    before = _office_pids(app)
    instance = win32com.client.DispatchEx(f"{app}.Application")
    pid = find_instance_pid(app, instance, before)
    try:
        instance.AutomationSecurity = _FORCE_DISABLE_MACROS
        if app == "Word":
            instance.Visible = False
            instance.DisplayAlerts = 0  # wdAlertsNone
        elif app == "PowerPoint":
            instance.DisplayAlerts = 1  # ppAlertsNone; PowerPoint refuses Visible = False
        else:
            instance.Visible = False
            instance.DisplayAlerts = False
            instance.EnableEvents = False
    except Exception:
        # Never open a file in an instance that may still run macros.
        if pid is not None:
            kill_process(pid)
        raise
    return instance, pid


def _open(app: str, instance, path: Path):
    if app == "Word":
        return instance.Documents.Open(
            FileName=str(path),
            ConfirmConversions=False,
            ReadOnly=True,
            AddToRecentFiles=False,
            PasswordDocument=_DUMMY_PASSWORD,
            PasswordTemplate=_DUMMY_PASSWORD,
            Visible=False,
            NoEncodingDialog=True,
        )
    if app == "PowerPoint":
        return instance.Presentations.Open(f"{path}::{_DUMMY_PASSWORD}::", ReadOnly=-1, Untitled=0, WithWindow=0)
    return instance.Workbooks.Open(
        str(path),
        UpdateLinks=0,
        ReadOnly=True,
        Password=_DUMMY_PASSWORD,
        IgnoreReadOnlyRecommended=True,
        AddToMru=False,
        Notify=False,
    )


def _save(app: str, document, target: Path, file_format: int) -> None:
    if app == "Word":
        document.SaveAs2(FileName=str(target), FileFormat=file_format, AddToRecentFiles=False)
    else:
        document.SaveAs(str(target), file_format)


def _close(app: str, document) -> None:
    if app == "Word":
        document.Close(SaveChanges=0)
    elif app == "PowerPoint":
        document.Close()
    else:
        document.Close(SaveChanges=False)


def _quit(app: str, instance) -> None:
    if app == "Word":
        instance.Quit(0)  # wdDoNotSaveChanges
    else:
        instance.Quit()


def _reap(app: str, pid: int | None) -> None:
    """Office sometimes keeps running after Quit; kill that instance (and only that one)."""
    if pid is None:
        return
    deadline = time.monotonic() + _QUIT_GRACE_SECONDS
    while pid in _office_pids(app):
        if time.monotonic() >= deadline:
            logging.getLogger(__name__).warning("Microsoft %s did not exit after Quit; killing PID %d", app, pid)
            kill_process(pid)
            return
        time.sleep(0.2)


def convert_with_office(source: Path, work_dir: Path, on_started: Callable[[int | None], None] | None = None) -> Path:
    """Save source as OOXML in work_dir with its Office app and return the new file.

    source must already be a private copy (the caller copies it into work_dir). on_started receives the
    instance's PID before the file is opened, so a watchdog can kill a hung instance."""
    import pythoncom
    import pywintypes

    app = OFFICE_EXTENSIONS[source.suffix.lower()]
    file_format, extension = _SAVE_FORMATS[app]
    target = work_dir / (source.stem + extension)
    logger = logging.getLogger(__name__)
    pythoncom.CoInitialize()
    instance = pid = None
    try:
        instance, pid = start_office(app)
        if on_started:
            on_started(pid)
        document = _open(app, instance, source)
        try:
            _save(app, document, target, file_format)
        finally:
            try:
                _close(app, document)
            except pywintypes.com_error:
                logger.warning("Microsoft %s could not close the document", app)
    except pywintypes.com_error as error:
        # COM messages include the file path; keep it out of results and logs.
        logger.warning("Microsoft %s failed: HRESULT %s", app, error.args[0] if error.args else "?")
        raise RuntimeError(f"Microsoft {app} could not open this file.") from None
    finally:
        if instance is not None:
            try:
                _quit(app, instance)
            except pywintypes.com_error:
                logger.warning("Microsoft %s did not quit cleanly", app)
        instance = None
        _reap(app, pid)
        pythoncom.CoUninitialize()
    return target
