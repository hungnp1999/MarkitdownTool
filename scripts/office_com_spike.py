"""Spike for MarkitdownTool #11 / TC-4 (#24): check how Microsoft Office COM behaves on this machine.

Run from the repository root with the project venv (pywin32 must be installed):

    .\\.venv\\Scripts\\python.exe -m pip install pywin32==312
    .\\.venv\\Scripts\\python.exe scripts\\office_com_spike.py --fixtures <folder with sample.doc, .rtf, .odt, .ppt, .odp, .ods>

Every check uses its own Office instance (DispatchEx), so Office windows you already have open are not touched.
Hung instances are killed by PID only. Results are printed and written to spike_result.md / spike_result.json
in the output folder (default: a new temporary folder).

This script is a standalone diagnostic: it is not imported by the app and is not packaged.
"""

import argparse
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

try:
    import pythoncom
    import pywintypes
    import win32com.client
    import win32gui
    import win32process
except ImportError:
    sys.exit("pywin32 is not installed. Run: .\\.venv\\Scripts\\python.exe -m pip install pywin32==312")

TIMEOUT_SECONDS = 60
FORCE_DISABLE = 3  # msoAutomationSecurityForceDisable

# ext -> (app, SaveAs format, output extension)
FORMATS = {
    ".doc": ("Word", 12, ".docx"),
    ".rtf": ("Word", 12, ".docx"),
    ".odt": ("Word", 12, ".docx"),
    ".docx": ("Word", 12, ".docx"),
    ".ppt": ("PowerPoint", 24, ".pptx"),
    ".odp": ("PowerPoint", 24, ".pptx"),
    ".ods": ("Excel", 51, ".xlsx"),
}
TASKKILL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
TASKLIST = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tasklist.exe"
EXE_NAMES = {"Word": "WINWORD.EXE", "PowerPoint": "POWERPNT.EXE", "Excel": "EXCEL.EXE"}

results = []


def record(item, check, passed, detail="", seconds=None):
    status = {True: "PASS", False: "FAIL", None: "SKIP"}[passed]
    results.append({"item": item, "check": check, "status": status, "detail": detail, "seconds": seconds})
    timing = f" ({seconds:.1f}s)" if seconds is not None else ""
    print(f"[{status}] {item} {check}{timing} {detail}".rstrip(), flush=True)


# --- Office instances ------------------------------------------------------------------

def pids_of(app):
    output = subprocess.run(
        [str(TASKLIST), "/FI", f"IMAGENAME eq {EXE_NAMES[app]}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, check=False,
    ).stdout
    pids = set()
    for line in output.splitlines():
        parts = [part.strip('"') for part in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def find_pid(app, instance, before):
    """Return (pid, method). Tries the window handle first, then the process list difference."""
    try:
        if app == "Word":
            marker = f"mdt-{uuid.uuid4()}"
            instance.Caption = marker
            hwnd = win32gui.FindWindow("OpusApp", marker)
        elif app == "PowerPoint":
            hwnd = instance.HWND
        else:
            hwnd = instance.Hwnd
        if hwnd:
            return win32process.GetWindowThreadProcessId(hwnd)[1], "window handle"
    except Exception:  # noqa: BLE001 - diagnostic, try the next method
        pass
    new = pids_of(app) - before
    if len(new) == 1:
        return new.pop(), "process list difference"
    return None, f"not found ({len(new)} new processes)"


def start(app):
    before = pids_of(app)
    instance = win32com.client.DispatchEx(f"{app}.Application")
    pid, method = find_pid(app, instance, before)
    instance.AutomationSecurity = FORCE_DISABLE
    if app == "Word":
        instance.Visible = False
        instance.DisplayAlerts = 0  # wdAlertsNone
    elif app == "PowerPoint":
        instance.DisplayAlerts = 1  # ppAlertsNone (2 is ppAlertsAll); never set Visible=False
    else:
        instance.Visible = False
        instance.DisplayAlerts = False
        instance.EnableEvents = False
    return instance, pid, method


def open_document(app, instance, path, password="x"):
    if app == "Word":
        return instance.Documents.Open(
            FileName=str(path), ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False,
            PasswordDocument=password, PasswordTemplate=password, Visible=False, NoEncodingDialog=True,
        )
    if app == "PowerPoint":
        return instance.Presentations.Open(f"{path}::{password}::", ReadOnly=-1, Untitled=0, WithWindow=0)
    return instance.Workbooks.Open(
        str(path), UpdateLinks=0, ReadOnly=True, Password=password, IgnoreReadOnlyRecommended=True,
        AddToMru=False, Notify=False,
    )


def close_document(app, document):
    if app == "Word":
        document.Close(SaveChanges=0)
    elif app == "PowerPoint":
        document.Close()
    else:
        document.Close(SaveChanges=False)


def quit_instance(app, instance):
    try:
        instance.Quit(0) if app == "Word" else instance.Quit()
    except Exception:  # noqa: BLE001
        pass


def kill(pid):
    return subprocess.run([str(TASKKILL), "/PID", str(pid), "/T", "/F"], capture_output=True, check=False).returncode == 0


def run_isolated(app, action, timeout=TIMEOUT_SECONDS):
    """Run action(instance) in a fresh Office instance on its own thread; kill that PID on timeout.

    Returns dict(ok, value, error, seconds, pid, pid_method, timed_out, killed, version)."""
    state = {"pid": None, "pid_method": "", "ok": False, "value": None, "error": "", "version": ""}

    def worker():
        pythoncom.CoInitialize()
        instance = None
        try:
            instance, state["pid"], state["pid_method"] = start(app)
            state["version"] = f"{instance.Version} build {getattr(instance, 'Build', '?')}"
            state["value"] = action(instance)
            state["ok"] = True
        except pywintypes.com_error as error:
            state["error"] = f"COM error: {error.args[1] if len(error.args) > 1 else error}"
            if len(error.args) > 2 and error.args[2]:
                state["error"] += f" / {error.args[2][2]}"
        except Exception as error:  # noqa: BLE001
            state["error"] = f"{type(error).__name__}: {error}"
        finally:
            if instance is not None:
                quit_instance(app, instance)
            pythoncom.CoUninitialize()

    began = time.perf_counter()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    state["timed_out"] = thread.is_alive()
    state["killed"] = False
    if state["timed_out"] and state["pid"]:
        state["killed"] = kill(state["pid"])
        thread.join(15)
    state["seconds"] = time.perf_counter() - began
    if not state["timed_out"] and state["pid"] and state["pid"] in pids_of(app):
        time.sleep(2)  # Quit is asynchronous
        if state["pid"] in pids_of(app):
            state["error"] += " (instance still running after Quit; killed)"
            kill(state["pid"])
    return state


def convert(app, source, target_dir, password="x"):
    _, file_format, out_ext = FORMATS[source.suffix.lower()]
    target = target_dir / (source.stem + "-" + source.suffix.lstrip(".") + out_ext)

    def action(instance):
        document = open_document(app, instance, source, password)
        try:
            if app == "Word":
                document.SaveAs2(FileName=str(target), FileFormat=file_format, AddToRecentFiles=False)
            else:
                document.SaveAs(str(target), file_format)
        finally:
            close_document(app, document)
        return target

    return run_isolated(app, action)


# --- Fixtures generated on the fly --------------------------------------------------------

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def write_docx(path, image_url=None, template_url=None):
    """Minimal .docx with an externally linked picture and/or an attached remote template."""
    drawing = ""
    doc_rels = [f'<Relationship Id="rIdSettings" Type="{R_NS}/settings" Target="settings.xml"/>']
    if image_url:
        doc_rels.append(f'<Relationship Id="rIdImg" Type="{R_NS}/image" Target="{image_url}" TargetMode="External"/>')
        drawing = (
            '<w:p><w:r><w:drawing><wp:inline><wp:extent cx="952500" cy="952500"/><wp:docPr id="1" name="x"/>'
            '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            '<pic:pic><pic:nvPicPr><pic:cNvPr id="1" name="x.png"/><pic:cNvPicPr/></pic:nvPicPr>'
            '<pic:blipFill><a:blip r:link="rIdImg"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="952500" cy="952500"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>'
            "</wp:inline></w:drawing></w:r></w:p>"
        )
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f"<w:body><w:p><w:r><w:t>Cân điện tử</w:t></w:r></w:p>{drawing}<w:sectPr/></w:body></w:document>"
    )
    settings_rels = ""
    template = ""
    if template_url:
        template = '<w:attachedTemplate r:id="rIdTpl"/>'
        settings_rels = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{REL_NS}">'
            f'<Relationship Id="rIdTpl" Type="{R_NS}/attachedTemplate" Target="{template_url}" TargetMode="External"/>'
            "</Relationships>"
        )
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/settings.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/></Types>'
        ),
        "_rels/.rels": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{REL_NS}">'
            f'<Relationship Id="rId1" Type="{R_NS}/officeDocument" Target="word/document.xml"/></Relationships>'
        ),
        "word/document.xml": document,
        "word/_rels/document.xml.rels": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{REL_NS}">'
            + "".join(doc_rels) + "</Relationships>"
        ),
        "word/settings.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:settings xmlns:w="{W_NS}" xmlns:r="{R_NS}">{template}</w:settings>'
        ),
    }
    if settings_rels:
        parts["word/_rels/settings.xml.rels"] = settings_rels
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)


def write_rtf(path, image_url=None, template_url=None):
    body = [r"{\rtf1\ansi\deff0{\fonttbl{\f0 Arial;}}"]
    if template_url:
        body.append(r"{\*\template " + template_url + "}")
    body.append(r"C\u226?n \u273?i\u7879?n t\u7917?\par ")
    if image_url:
        body.append(r'{\field{\*\fldinst {INCLUDEPICTURE "' + image_url + r'" \\d}}{\fldrslt }}\par ')
    body.append("}")
    path.write_text("".join(body), encoding="ascii")


class CountingHandler(http.server.BaseHTTPRequestHandler):
    hits = {}
    lock = threading.Lock()

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._count
        raise AttributeError(name)

    def _count(self):
        with self.lock:
            self.hits[self.path] = self.hits.get(self.path, 0) + 1
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def hits(prefix):
    with CountingHandler.lock:
        return sum(count for path, count in CountingHandler.hits.items() if path.startswith(prefix))


# --- Checks -------------------------------------------------------------------------------

def check_pid_detection():
    for app in ("Word", "PowerPoint", "Excel"):
        state = run_isolated(app, lambda instance: None)
        if state["error"].startswith("COM error") and not state["pid"]:
            record("1", f"{app}: DispatchEx + PID", None, f"{app} not available: {state['error']}")
            continue
        record("1", f"{app}: DispatchEx + PID", bool(state["pid"]),
               f"pid={state['pid']} via {state['pid_method']}; Office {state['version']}", state["seconds"])


def markdown_of(path):
    try:
        from markitdown import MarkItDown
    except ImportError:
        return None
    return MarkItDown().convert(str(path)).text_content


def check_fixtures(fixtures, work):
    timings = {}
    for name in ("sample.doc", "sample.rtf", "sample.odt", "sample.ppt", "sample.odp", "sample.ods"):
        source = fixtures / name
        app = FORMATS[source.suffix][0]
        if not source.exists():
            record("2", f"{name} -> OOXML", None, "fixture missing")
            continue
        state = convert(app, source, work)
        if not state["ok"]:
            record("2", f"{name} -> OOXML", False, state["error"] or "timed out", state["seconds"])
            continue
        timings.setdefault(app, []).append(state["seconds"])
        text = markdown_of(state["value"])
        if text is None:
            record("2", f"{name} -> OOXML", True, "markitdown not importable; content not checked", state["seconds"])
            continue
        expected = ["Cân điện tử", "TB-01"] + (["| TB-01 | Cân điện tử |"] if name == "sample.doc" else [])
        missing = [item for item in expected if item not in text]
        (work / (name + ".md")).write_text(text, encoding="utf-8")
        record("2", f"{name} -> OOXML -> Markdown", not missing,
               f"missing {missing}" if missing else "content OK", state["seconds"])
    for app, values in timings.items():
        record("7", f"{app}: average time per file", True, f"{sum(values) / len(values):.1f}s over {len(values)} files")


def make_password_files(work):
    files = {}

    def word(instance):
        document = instance.Documents.Add()
        document.Content.Text = "Cân điện tử"
        document.Password = "secret"  # SaveAs2(Password=...) left the .docx unencrypted
        for ext, fmt in ((".docx", 16), (".doc", 0)):
            target = work / f"locked{ext}"
            document.SaveAs2(FileName=str(target), FileFormat=fmt, AddToRecentFiles=False)
            files[target] = "Word"
        document.Close(SaveChanges=0)

    def excel(instance):
        workbook = instance.Workbooks.Add()
        workbook.Worksheets(1).Range("A1").Value = "Cân điện tử"
        target = work / "locked.xlsx"
        workbook.SaveAs(str(target), 51, "secret")
        workbook.Close(SaveChanges=False)
        files[target] = "Excel"

    def powerpoint(instance):
        presentation = instance.Presentations.Add(WithWindow=0)
        presentation.Slides.Add(1, 1).Shapes(1).TextFrame.TextRange.Text = "Cân điện tử"
        presentation.Password = "secret"
        target = work / "locked.pptx"
        presentation.SaveAs(str(target), 24)
        presentation.Close()
        files[target] = "PowerPoint"

    for app, action in (("Word", word), ("Excel", excel), ("PowerPoint", powerpoint)):
        state = run_isolated(app, action)
        if not state["ok"]:
            record("3", f"{app}: create password-protected file", None, state["error"] or "timed out")
    return files


def check_passwords(work):
    for path, app in make_password_files(work).items():
        state = run_isolated(app, lambda instance, p=path, a=app: close_document(a, open_document(a, instance, p)), 30)
        passed = not state["ok"] and not state["timed_out"]
        detail = "raised: " + state["error"] if passed else ("hung (dialog?), killed" if state["timed_out"] else "opened with a wrong password?")
        record("3", f"{path.name}: wrong password raises, no dialog", passed, detail, state["seconds"])


def check_corrupt(fixtures, work):
    source = fixtures / "sample.doc"
    if not source.exists():
        record("4", "corrupt .doc", None, "fixture missing")
        return
    data = source.read_bytes()
    broken = work / "broken.doc"
    broken.write_bytes(data[: len(data) // 2])
    state = convert("Word", broken, work)
    if state["timed_out"]:
        record("4", "corrupt .doc: hang -> kill by PID", state["killed"], f"pid={state['pid']}, killed={state['killed']}", state["seconds"])
    else:
        record("4", "corrupt .doc: raises or converts without hanging", True,
               "converted (repaired)" if state["ok"] else "raised: " + state["error"], state["seconds"])


def check_network(work):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CountingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        cases = []
        path = work / "net-image.docx"
        write_docx(path, image_url=f"{base}/docx-image/x.png")
        cases.append((path, "/docx-image/", "docx linked picture"))
        path = work / "net-template.docx"
        write_docx(path, template_url=f"{base}/docx-template/t.dotm")
        cases.append((path, "/docx-template/", "docx remote attached template"))
        path = work / "net-image.rtf"
        write_rtf(path, image_url=f"{base}/rtf-image/x.png")
        cases.append((path, "/rtf-image/", "rtf INCLUDEPICTURE"))
        path = work / "net-template.rtf"
        write_rtf(path, template_url=f"{base}/rtf-template/t.dot")
        cases.append((path, "/rtf-template/", "rtf \\template"))

        # A .doc with a linked picture, saved by Word itself (requests made while creating it are not counted).
        doc_source = work / "net-doc-source.rtf"
        write_rtf(doc_source, image_url=f"{base}/doc-image/x.png")
        doc_path = work / "net-image.doc"

        def save_as_doc(instance):
            document = instance.Documents.Open(FileName=str(doc_source), ConfirmConversions=False, AddToRecentFiles=False, Visible=False)
            document.SaveAs2(FileName=str(doc_path), FileFormat=0, AddToRecentFiles=False)
            document.Close(SaveChanges=0)

        made = run_isolated("Word", save_as_doc)
        if made["ok"]:
            cases.append((doc_path, "/doc-image/", "doc INCLUDEPICTURE"))
        else:
            reason = "hung, timed out" if made["timed_out"] else made["error"]
            record("5", "doc INCLUDEPICTURE", None, f"could not create the .doc fixture: {reason}", made["seconds"])

        for path, prefix, label in cases:
            before = hits(prefix)
            state = convert("Word", path, work)
            time.sleep(3)
            requests = hits(prefix) - before
            if state["timed_out"]:
                outcome = f"hung, timed out after {TIMEOUT_SECONDS}s, killed={state['killed']}"
            else:
                outcome = "converted" if state["ok"] else f"failed: {state['error']}"
            # A file that failed to open proves nothing about network access.
            passed = False if requests else (True if state["ok"] else None)
            record("5", f"{label}: no network request", passed, f"{requests} request(s); {outcome}", state["seconds"])
        record("5", "all requests seen by the local server", True, json.dumps(CountingHandler.hits))
    finally:
        server.shutdown()


def check_macro(work):
    marker = Path(tempfile.gettempdir()) / f"mdt_macro_ran_{uuid.uuid4().hex}.txt"
    code = (
        "Sub AutoOpen()\r\n"
        f'    Open "{marker}" For Output As #1\r\n'
        '    Print #1, "macro ran"\r\n'
        "    Close #1\r\n"
        "End Sub\r\n"
    )
    path = work / "macro.doc"

    def create(instance):
        document = instance.Documents.Add()
        document.Content.Text = "Cân điện tử"
        document.VBProject.VBComponents.Add(1).CodeModule.AddFromString(code)
        document.SaveAs2(FileName=str(path), FileFormat=0, AddToRecentFiles=False)
        document.Close(SaveChanges=0)

    state = run_isolated("Word", create)
    if not state["ok"]:
        record("6", "macro AutoOpen does not run", None,
               "could not create the macro fixture. To run this check, temporarily turn on Word > Options > Trust Center > "
               "Trust Center Settings > Macro Settings > 'Trust access to the VBA project object model', then turn it off again. "
               + state["error"])
        return
    state = convert("Word", path, work)
    time.sleep(2)
    ran = marker.exists()
    if ran:
        marker.unlink()
    record("6", "macro AutoOpen does not run", not ran,
           ("macro RAN" if ran else "marker file absent") + ("; converted" if state["ok"] else f"; failed: {state['error']}"),
           state["seconds"])


def write_report(out):
    lines = [
        "## TC-4 Office COM spike result",
        "",
        f"Machine: {os.environ.get('COMPUTERNAME', '?')} · Python {sys.version.split()[0]} · {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "| # | Check | Result | Time | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in results:
        seconds = f"{item['seconds']:.1f}s" if item["seconds"] is not None else ""
        detail = " ".join(item["detail"].replace("|", "\\|").split())
        lines.append(f"| {item['item']} | {item['check']} | {item['status']} | {seconds} | {detail} |")
    (out / "spike_result.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "spike_result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nReport: {out / 'spike_result.md'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixtures", type=Path, default=Path("tests") / "fixtures", help="folder with the sample.* fixtures")
    parser.add_argument("--out", type=Path, help="output folder (default: new temporary folder)")
    parser.add_argument("--skip-macro", action="store_true", help="skip check 6 (needs VBA project access to build the fixture)")
    args = parser.parse_args()
    # Office resolves relative paths against its own working folder, so pass absolute ones.
    args.fixtures = args.fixtures.resolve()
    out = (args.out or Path(tempfile.mkdtemp(prefix="mdt-spike-"))).resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = out / "work"
    work.mkdir(exist_ok=True)
    print(f"Output folder: {out}\n")

    checks = [
        ("1", check_pid_detection, ()),
        ("2", check_fixtures, (args.fixtures, work)),
        ("3", check_passwords, (work,)),
        ("4", check_corrupt, (args.fixtures, work)),
        ("5", check_network, (work,)),
    ]
    if not args.skip_macro:
        checks.append(("6", check_macro, (work,)))
    for item, check, check_args in checks:
        try:
            check(*check_args)
        except Exception as error:  # noqa: BLE001 - keep going so the report covers every check
            record(item, f"{check.__name__} crashed", False, f"{type(error).__name__}: {error}")
    write_report(out)
    return 1 if any(item["status"] == "FAIL" for item in results) else 0


if __name__ == "__main__":
    sys.exit(main())
