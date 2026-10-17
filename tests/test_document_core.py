import io
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

from document_core import (
    BatchResult,
    FileClassification,
    FileStatus,
    ProcessingMode,
    DEFAULT_IMAGE_DPI,
    ScanLimitExceeded,
    classify_file,
    prepare_image_input,
    process_batch,
    scan_folder,
)


class ClassificationTests(unittest.TestCase):
    @patch("document_core.pdf_needs_ocr", return_value=True)
    def test_scanned_pdf_is_ocr_only_in_markdown_preflight(self, _needs_ocr):
        result = classify_file("scan.pdf")

        self.assertTrue(result.supported)
        self.assertTrue(result.ocr_eligible)
        self.assertFalse(result.markdown_eligible)
        self.assertIn("OCR", result.reason)

    @patch("pypdfium2.PdfDocument")
    def test_pdf_with_text_on_every_page_does_not_need_ocr(self, open_pdf):
        document = MagicMock()
        document.__enter__.return_value = document
        document.__len__.return_value = 2
        first_page, second_page = MagicMock(), MagicMock()
        first_text, second_text = MagicMock(), MagicMock()
        document.__getitem__.side_effect = [first_page, second_page]
        first_page.get_textpage.return_value = first_text
        second_page.get_textpage.return_value = second_text
        first_text.get_text_bounded.return_value = "First page text"
        second_text.get_text_bounded.return_value = "Second page text"
        open_pdf.return_value = document

        from document_core import pdf_needs_ocr

        self.assertFalse(pdf_needs_ocr(Path("text.pdf")))
        self.assertTrue(first_page.close.called)
        self.assertTrue(second_page.close.called)
        self.assertTrue(first_text.close.called)
        self.assertTrue(second_text.close.called)

    @patch("pypdfium2.PdfDocument")
    def test_pdf_with_a_page_without_text_needs_ocr(self, open_pdf):
        document = MagicMock()
        document.__enter__.return_value = document
        document.__len__.return_value = 2
        first_page, second_page = MagicMock(), MagicMock()
        first_text, second_text = MagicMock(), MagicMock()
        document.__getitem__.side_effect = [first_page, second_page]
        first_page.get_textpage.return_value = first_text
        second_page.get_textpage.return_value = second_text
        first_text.get_text_bounded.return_value = "Page text"
        second_text.get_text_bounded.return_value = ""
        open_pdf.return_value = document

        from document_core import pdf_needs_ocr

        self.assertTrue(pdf_needs_ocr(Path("mixed.pdf")))
        self.assertTrue(first_page.close.called)
        self.assertTrue(second_page.close.called)
        self.assertTrue(first_text.close.called)
        self.assertTrue(second_text.close.called)

    @patch("pypdfium2.PdfDocument", side_effect=ValueError("invalid PDF"))
    def test_unreadable_pdf_defaults_to_ocr(self, _open_pdf):
        from document_core import pdf_needs_ocr

        self.assertTrue(pdf_needs_ocr(Path("invalid.pdf")))

    def test_image_files_need_ocr(self):
        for name in ("photo.png", "scan.JPG", "page.tiff", "old.bmp"):
            with self.subTest(name=name):
                result = classify_file(name)
                self.assertEqual(
                    (result.supported, result.needs_ocr, result.markdown_eligible, result.ocr_eligible),
                    (True, True, False, True),
                )
                self.assertEqual(result.reason, "Ảnh cần OCR trước khi tạo Markdown.")

    def test_legacy_excel_and_csv_are_ready(self):
        for name in ("old.xls", "data.CSV"):
            with self.subTest(name=name):
                result = classify_file(name)
                self.assertEqual((result.supported, result.needs_ocr, result.markdown_eligible), (True, False, True))

    def test_other_formats_are_not_supported(self):
        result = classify_file("archive.zip")

        self.assertFalse(result.supported)
        self.assertFalse(result.markdown_eligible)
        self.assertFalse(result.ocr_eligible)


class ImageInputTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def save(self, name, mode="RGB", **options):
        from PIL import Image

        path = self.root / name
        Image.new(mode, (40, 20), (255, 255, 255, 0) if mode == "RGBA" else "white").save(path, **options)
        return path

    def test_image_with_resolution_passes_through_unchanged(self):
        path = self.save("dpi.jpg", dpi=(300, 300))
        with open(path, "rb") as stream:
            stream.read(5)
            stream.seek(0)
            result, options = prepare_image_input(path, stream, self.root)
            self.assertIs(result, stream)
            self.assertEqual((stream.tell(), options), (0, {}))
        self.assertEqual(prepare_image_input(path, None, self.root), (str(path), {}))

    def test_image_without_resolution_gets_the_default_dpi(self):
        path = self.save("nodpi.jpg")
        self.assertEqual(prepare_image_input(path, None, self.root)[1], {"image_dpi": DEFAULT_IMAGE_DPI})

    def test_transparent_image_is_flattened_onto_white(self):
        from PIL import Image

        path = self.save("alpha.png", "RGBA")
        with open(path, "rb") as stream:
            result, options = prepare_image_input(path, stream, self.root)
        self.assertEqual(options, {"image_dpi": DEFAULT_IMAGE_DPI})
        with Image.open(result) as flattened:
            self.assertEqual((flattened.format, flattened.mode), ("TIFF", "RGB"))
            self.assertEqual(flattened.getpixel((0, 0)), (255, 255, 255))


class BatchProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp_dir.name)
        self.ocr_dir = self.project_dir / "ocrpdf"
        self.markdown_dir = self.project_dir / "markdown"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_ocr_mode_does_not_create_markdown(self):
        source = self.project_dir / "scan.pdf"
        classification = FileClassification(source, True, True, False, True)
        with patch("document_core.run_ocr", return_value=self.ocr_dir / "scan.pdf") as run_ocr:
            with patch("document_core.convert_markdown") as convert:
                result = process_batch(
                    [source], ProcessingMode.OCR, ["eng"], self.project_dir,
                    self.ocr_dir, self.markdown_dir, classifications=[classification]
                )

        run_ocr.assert_called_once()
        convert.assert_not_called()
        self.assertEqual(result.files[0].status, FileStatus.SUCCESS)
        self.assertEqual(result.files[0].stage, "OCR")

    def test_successful_batch_writes_processing_info_logs(self):
        source = self.project_dir / "report.docx"
        classification = FileClassification(source, True, False, True, False)
        with patch("document_core.convert_markdown", return_value=self.markdown_dir / "report.md"):
            with self.assertLogs("document_core", level="INFO") as captured:
                result = process_batch(
                    [source], ProcessingMode.MARKDOWN, [], self.project_dir,
                    self.ocr_dir, self.markdown_dir, classifications=[classification]
                )

        self.assertEqual(result.files[0].status, FileStatus.SUCCESS)
        self.assertTrue(any("Batch started" in record for record in captured.output))
        self.assertTrue(any("File completed: report.docx" in record for record in captured.output))
        self.assertTrue(any("Batch completed" in record for record in captured.output))

    def test_markdown_mode_skips_scanned_pdf_and_processes_supported_file(self):
        scan = self.project_dir / "scan.pdf"
        document = self.project_dir / "report.docx"
        classifications = [
            FileClassification(scan, True, True, False, True, "PDF cần OCR trước khi tạo Markdown."),
            FileClassification(document, True, False, True, False),
        ]
        with patch("document_core.run_ocr") as run_ocr:
            with patch("document_core.convert_markdown", return_value=self.markdown_dir / "report.md"):
                result = process_batch(
                    [scan, document], ProcessingMode.MARKDOWN, [], self.project_dir,
                    self.ocr_dir, self.markdown_dir, classifications=classifications
                )

        run_ocr.assert_not_called()
        self.assertEqual([item.status for item in result.files], [FileStatus.SKIPPED, FileStatus.SUCCESS])

    def test_cancel_stops_before_next_file(self):
        sources = [self.project_dir / "one.pdf", self.project_dir / "two.pdf"]
        classifications = [
            FileClassification(path, True, True, False, True)
            for path in sources
        ]
        cancel_event = Event()

        def finish_first_file(*_args):
            cancel_event.set()
            return self.ocr_dir / "one.pdf"

        with patch("document_core.run_ocr", side_effect=finish_first_file) as run_ocr:
            result = process_batch(
                sources, ProcessingMode.OCR, ["eng"], self.project_dir,
                self.ocr_dir, self.markdown_dir, cancel_event=cancel_event,
                classifications=classifications
            )

        run_ocr.assert_called_once()
        self.assertTrue(result.cancelled)
        self.assertEqual(len(result.files), 1)

    def test_open_source_stream_is_passed_to_the_converter(self):
        source = self.project_dir / "report.docx"
        classification = FileClassification(source, True, False, True, False)
        stream = io.BytesIO(b"x")
        opened = []

        @contextmanager
        def open_source(path):
            opened.append(path)
            yield stream

        with patch("document_core.convert_markdown", return_value=self.markdown_dir / "report.md") as convert:
            process_batch(
                [source], ProcessingMode.MARKDOWN, [], self.project_dir,
                self.ocr_dir, self.markdown_dir, classifications=[classification], open_source=open_source
            )

        self.assertEqual(opened, [source])
        self.assertIs(convert.call_args.kwargs["stream"], stream)

    def test_open_source_failure_marks_only_that_file_failed(self):
        source = self.project_dir / "report.docx"
        classification = FileClassification(source, True, False, True, False)

        def open_source(_path):
            raise PermissionError("moved")

        with patch("document_core.convert_markdown") as convert:
            result = process_batch(
                [source], ProcessingMode.MARKDOWN, [], self.project_dir,
                self.ocr_dir, self.markdown_dir, classifications=[classification], open_source=open_source
            )

        convert.assert_not_called()
        self.assertEqual((result.files[0].status, result.files[0].message), (FileStatus.FAILED, "moved"))


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "one" / "two").mkdir(parents=True)
        for name in ("a.pdf", "notes.txt", "~$a.docx", "one/b.docx", "one/two/c.xlsx"):
            (self.root / name).write_bytes(b"x")

    def tearDown(self):
        self.temp_dir.cleanup()

    def names(self, **options):
        return [path.name for path in scan_folder(self.root, **options)]

    def test_matches_collect_supported_files(self):
        self.assertEqual(self.names(), ["a.pdf"])
        self.assertEqual(self.names(recursive=True), ["a.pdf", "b.docx", "c.xlsx"])

    def test_unsupported_files_are_listed_without_hidden_and_lock_files(self):
        (self.root / ".cache").mkdir()
        for name in (".cache/x.bin", ".cache/hidden.pdf", ".env", "one/photo.png"):
            (self.root / name).write_bytes(b"x")
        self.assertEqual(
            self.names(recursive=True, include_unsupported=True),
            ["hidden.pdf", "a.pdf", "notes.txt", "b.docx", "photo.png", "c.xlsx"],
        )

    @unittest.skipUnless(os.name == "nt", "hidden attribute is Windows-only")
    def test_hidden_and_system_files_are_not_listed(self):
        import ctypes

        for name, attribute in (("Thumbs.db", 0x2), ("desktop.ini", 0x4)):
            (self.root / name).write_bytes(b"x")
            ctypes.windll.kernel32.SetFileAttributesW(str(self.root / name), attribute)
        self.assertEqual(self.names(include_unsupported=True), ["a.pdf", "notes.txt"])

    def test_depth_limit(self):
        self.assertEqual(self.names(recursive=True, max_depth=2), ["a.pdf", "b.docx", "c.xlsx"])
        self.assertEqual(self.names(max_depth=0), ["a.pdf"])
        with self.assertRaises(ScanLimitExceeded) as raised:
            scan_folder(self.root, recursive=True, max_depth=1)
        self.assertEqual((raised.exception.limit, raised.exception.value), ("max_scan_depth", 1))

    def test_file_limit_stops_the_scan(self):
        self.assertEqual(len(self.names(recursive=True, max_files=3)), 3)
        with self.assertRaises(ScanLimitExceeded) as raised:
            scan_folder(self.root, recursive=True, max_files=2)
        self.assertEqual(raised.exception.limit, "max_files")

    @unittest.skipUnless(os.name == "nt", "junctions are Windows-only")
    def test_linked_folders_are_not_entered(self):
        import _winapi

        outside = Path(self.temp_dir.name + "_outside")
        outside.mkdir()
        self.addCleanup(shutil.rmtree, outside)
        (outside / "secret.docx").write_bytes(b"x")
        _winapi.CreateJunction(str(outside), str(self.root / "link"))
        self.assertEqual(self.names(recursive=True), ["a.pdf", "b.docx", "c.xlsx"])


if __name__ == "__main__":
    unittest.main()