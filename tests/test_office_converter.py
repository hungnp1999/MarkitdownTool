import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import office_converter
from office_converter import OFFICE_EXTENSIONS, convert_with_office, convert_with_timeout, office_app_available


def fake_winreg(open_key):
    module = types.ModuleType("winreg")
    module.HKEY_CLASSES_ROOT = object()
    module.OpenKey = open_key
    return module


class OfficeAppAvailableTests(unittest.TestCase):
    def setUp(self):
        office_app_available.cache_clear()
        self.addCleanup(office_app_available.cache_clear)

    def test_extensions_map_to_their_office_app(self):
        self.assertEqual(
            OFFICE_EXTENSIONS,
            {".doc": "Word", ".rtf": "Word", ".odt": "Word", ".ppt": "PowerPoint", ".odp": "PowerPoint", ".ods": "Excel"},
        )

    def test_registered_app_is_available(self):
        open_key = MagicMock()
        with patch.object(office_converter.os, "name", "nt"), patch.dict(sys.modules, {"winreg": fake_winreg(open_key)}):
            self.assertTrue(office_app_available("Word"))
        self.assertEqual(open_key.call_args.args[1], r"Word.Application\CLSID")

    def test_missing_app_is_not_available(self):
        open_key = MagicMock(side_effect=OSError("not registered"))
        with patch.object(office_converter.os, "name", "nt"), patch.dict(sys.modules, {"winreg": fake_winreg(open_key)}):
            self.assertFalse(office_app_available("PowerPoint"))

    def test_result_is_cached(self):
        open_key = MagicMock()
        with patch.object(office_converter.os, "name", "nt"), patch.dict(sys.modules, {"winreg": fake_winreg(open_key)}):
            office_app_available("Excel")
            office_app_available("Excel")
        self.assertEqual(open_key.call_count, 1)

    def test_never_available_outside_windows(self):
        open_key = MagicMock()
        with patch.object(office_converter.os, "name", "posix"), patch.dict(sys.modules, {"winreg": fake_winreg(open_key)}):
            self.assertFalse(office_app_available("Word"))
        open_key.assert_not_called()


class ComError(Exception):
    pass


class FakeDocument:
    def __init__(self, events, save_error=None):
        self.events = events
        self.save_error = save_error

    def _save(self, *args, **kwargs):
        self.events.append("save")
        if self.save_error:
            raise self.save_error
        Path(kwargs.get("FileName") or args[0]).write_bytes(b"ooxml")

    SaveAs = SaveAs2 = _save

    def Close(self, *args, **kwargs):
        self.events.append("close")


class FakeCollection:
    def __init__(self, events, open_error=None, save_error=None):
        self.events, self.open_error, self.save_error = events, open_error, save_error

    def Open(self, *args, **kwargs):
        self.events.append(("open", args, kwargs))
        if self.open_error:
            raise self.open_error
        return FakeDocument(self.events, self.save_error)


class FakeOffice:
    """Records attribute writes and calls in order, like an Office Application object."""

    def __init__(self, events, **errors):
        object.__setattr__(self, "events", events)
        collection = FakeCollection(events, **errors)
        for name in ("Documents", "Presentations", "Workbooks"):
            object.__setattr__(self, name, collection)
        object.__setattr__(self, "Hwnd", 0)

    def __setattr__(self, name, value):
        self.events.append(("set", name, value))

    def Quit(self, *args):
        self.events.append("quit")


class ConvertWithOfficeTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.work = Path(self._temp.name)
        self.events = []
        self.pythoncom = types.SimpleNamespace(CoInitialize=MagicMock(), CoUninitialize=MagicMock())
        self.dispatch = MagicMock()
        client = types.ModuleType("win32com.client")
        client.DispatchEx = self.dispatch
        win32com = types.ModuleType("win32com")
        win32com.client = client
        modules = {
            "pythoncom": self.pythoncom,
            "pywintypes": types.SimpleNamespace(com_error=ComError),
            "win32com": win32com,
            "win32com.client": client,
            "win32gui": types.SimpleNamespace(FindWindow=MagicMock(return_value=0)),
            "win32process": types.SimpleNamespace(GetWindowThreadProcessId=MagicMock()),
        }
        for patcher in (
            patch.dict(sys.modules, modules),
            patch.object(office_converter, "_office_pids", side_effect=[set(), {1234}, set()]),
            patch.object(office_converter, "kill_process", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def office(self, **errors):
        self.dispatch.return_value = FakeOffice(self.events, **errors)

    def test_macros_are_disabled_before_the_file_is_opened(self):
        self.office()
        convert_with_office(self.work / "input.doc", self.work)
        opened = next(index for index, event in enumerate(self.events) if event[0] == "open")
        self.assertIn(("set", "AutomationSecurity", 3), self.events[:opened])
        self.assertIn(("set", "DisplayAlerts", 0), self.events[:opened])

    def test_output_is_ooxml_named_after_the_source(self):
        for name, app, expected in (("input.rtf", "Word", "input.docx"), ("input.odp", "PowerPoint", "input.pptx"), ("input.ods", "Excel", "input.xlsx")):
            with self.subTest(name=name):
                self.events.clear()
                office_converter._office_pids.side_effect = [set(), {1234}, set()]
                self.office()
                result = convert_with_office(self.work / name, self.work)
                self.assertEqual(result, self.work / expected)
                self.dispatch.assert_called_with(f"{app}.Application")
                self.assertEqual(self.events[-2:], ["close", "quit"])

    def test_powerpoint_alerts_use_pp_alerts_none(self):
        self.office()
        convert_with_office(self.work / "input.ppt", self.work)
        self.assertIn(("set", "DisplayAlerts", 1), self.events)
        self.assertNotIn(("set", "Visible", False), self.events)

    def test_open_failure_quits_and_hides_the_path(self):
        self.office(open_error=ComError(-2147352567, "Exception occurred.", (0, "Word", r"C:\secret\input.doc", None, 0, 0)))
        with self.assertRaises(RuntimeError) as raised:
            convert_with_office(self.work / "input.doc", self.work)
        self.assertEqual(str(raised.exception), "Microsoft Word could not open this file.")
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(self.events[-1], "quit")
        self.pythoncom.CoUninitialize.assert_called_once()

    def test_save_failure_still_closes_and_quits(self):
        self.office(save_error=ComError(-1, "save failed", None))
        with self.assertRaises(RuntimeError):
            convert_with_office(self.work / "input.doc", self.work)
        self.assertEqual(self.events[-3:], ["save", "close", "quit"])
        self.pythoncom.CoUninitialize.assert_called_once()

    def test_instance_still_running_after_quit_is_killed_by_pid(self):
        self.office()
        office_converter._office_pids.side_effect = [set(), {1234}, {1234}]
        with patch.object(office_converter, "_QUIT_GRACE_SECONDS", 0):
            convert_with_office(self.work / "input.doc", self.work)
        office_converter.kill_process.assert_called_once_with(1234)

    def test_on_started_receives_the_pid_before_open(self):
        self.office()
        seen = []
        convert_with_office(self.work / "input.doc", self.work, on_started=lambda pid: seen.append((pid, len(self.events))))
        opened = next(index for index, event in enumerate(self.events) if event[0] == "open")
        self.assertEqual(seen[0][0], 1234)
        self.assertLessEqual(seen[0][1], opened)


class ConvertWithTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        patcher = patch.object(office_converter, "kill_process", side_effect=lambda pid: self.release.set() or True)
        self.kill = patcher.start()
        self.addCleanup(patcher.stop)

    def hanging_converter(self, pid):
        def converter(source, work_dir, on_started):
            on_started(pid)
            self.release.wait(5)
            return work_dir / "never.docx"

        return converter

    def test_hung_instance_is_killed_by_its_pid(self):
        with self.assertRaises(TimeoutError) as raised:
            convert_with_timeout(Path("input.doc"), Path("work"), timeout=0.05, converter=self.hanging_converter(4321))
        self.assertEqual(str(raised.exception), "Microsoft Word did not respond in time.")
        self.kill.assert_called_once_with(4321)

    def test_unknown_pid_kills_nothing(self):
        with self.assertRaises(TimeoutError):
            convert_with_timeout(Path("input.ppt"), Path("work"), timeout=0.05, converter=self.hanging_converter(None))
        self.kill.assert_not_called()

    def test_result_and_errors_pass_through(self):
        result = convert_with_timeout(Path("input.ods"), Path("work"), converter=lambda s, w, on_started: w / "input.xlsx")
        self.assertEqual(result, Path("work") / "input.xlsx")

        def failing(source, work_dir, on_started):
            raise RuntimeError("Microsoft Excel could not open this file.")

        with self.assertRaisesRegex(RuntimeError, "could not open"):
            convert_with_timeout(Path("input.ods"), Path("work"), converter=failing)
        self.kill.assert_not_called()

    def test_default_timeout_is_sixty_seconds(self):
        self.assertEqual(office_converter.DEFAULT_OFFICE_TIMEOUT_SECONDS, 60)


if __name__ == "__main__":
    unittest.main()
