"""Conversions through the installed Microsoft Office (ADR-011). Skipped where the Office app is missing."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_paths import build_app_paths
from document_core import FileStatus, ProcessingMode, classify_file, output_path_for, process_batch
from office_converter import OFFICE_EXTENSIONS, office_app_available

FIXTURES = Path(__file__).parent / "fixtures"


class OfficeFixtureTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def copy_fixture(self, extension):
        # A content-only copy: no Zone.Identifier stream, whatever the checkout carries.
        target = self.root / f"sample{extension}"
        shutil.copyfile(FIXTURES / f"sample{extension}", target)
        return target

    def run_gui_batch(self, source):
        return process_batch(
            [source], ProcessingMode.MARKDOWN, [], self.root, self.root / "ocrpdf", self.root / "markdown",
            classifications=[classify_file(source)],
        )


class RealOfficeConversionTests(OfficeFixtureTest):
    def convert(self, extension):
        app = OFFICE_EXTENSIONS[extension]
        if not office_app_available(app):
            self.skipTest(f"needs Microsoft {app}")
        source = self.copy_fixture(extension)
        result = self.run_gui_batch(source)
        item = result.files[0]
        self.assertEqual(item.status, FileStatus.SUCCESS, item.message)
        self.assertEqual(item.outputs[0], output_path_for(source, self.root / "markdown", ".md"))
        return item.outputs[0].read_text(encoding="utf-8")

    def test_doc(self):
        markdown = self.convert(".doc")
        self.assertIn("Cân điện tử", markdown)
        self.assertIn("| TB-01 | Cân điện tử |", markdown)

    def test_rtf(self):
        self.assertIn("Cân điện tử", self.convert(".rtf"))

    def test_odt(self):
        self.assertIn("Cân điện tử", self.convert(".odt"))

    def test_ppt(self):
        self.assertIn("Cân điện tử", self.convert(".ppt"))

    def test_odp(self):
        self.assertIn("Cân điện tử", self.convert(".odp"))

    def test_ods(self):
        self.assertIn("Cân điện tử", self.convert(".ods"))

    @unittest.skip("needs a Word fixture with an AutoOpen macro, which requires VBA project access to create; spike check 6")
    def test_auto_open_macro_does_not_run(self):
        pass


class OfficeSafetyTests(OfficeFixtureTest):
    @unittest.skipUnless(os.name == "nt", "NTFS alternate data streams")
    def test_downloaded_file_is_skipped_without_starting_office(self):
        source = self.copy_fixture(".doc")
        with open(f"{source}:Zone.Identifier", "w", encoding="ascii") as stream:
            stream.write("[ZoneTransfer]\nZoneId=3\nHostUrl=https://example.com/sample.doc\n")
        with patch("document_core.convert_with_timeout") as converter, \
                patch("document_core.office_app_available", return_value=True):
            result = self.run_gui_batch(source)
        converter.assert_not_called()
        self.assertEqual(result.files[0].status, FileStatus.SKIPPED)

    def test_cli_refuses_every_office_fixture(self):
        from markitdown_cli import main

        sources = [self.copy_fixture(extension) for extension in OFFICE_EXTENSIONS]
        paths = build_app_paths(self.root, self.root / "local")
        for command in (["classify"], ["convert", "--mode", "markdown", "--output-dir", str(self.root / "out"), "--quiet"]):
            with self.subTest(command=command[0]):
                stdout = io.StringIO()
                with patch("app_runtime.prepare_runtime", return_value=paths), \
                        patch("document_core.office_app_available", return_value=True), \
                        patch("document_core.convert_with_timeout") as converter, \
                        contextlib.redirect_stdout(stdout):
                    main(command + [str(source) for source in sources] + ["--json"])
                converter.assert_not_called()
                files = json.loads(stdout.getvalue())["files"]
                self.assertEqual([item["reason_code"] for item in files], ["office_gui_only"] * len(sources))


if __name__ == "__main__":
    unittest.main()
