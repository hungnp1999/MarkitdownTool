import sys
import types
import unittest
from unittest.mock import MagicMock, patch

import office_converter
from office_converter import OFFICE_EXTENSIONS, office_app_available


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


if __name__ == "__main__":
    unittest.main()
