import io
import os
import sys
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import pdf_samples  # noqa: E402
from document_core import (
    BatchResult,
    FileClassification,
    FileStatus,
    ProcessingMode,
    DEFAULT_IMAGE_DPI,
    PdfTextReport,
    ScanLimitExceeded,
    analyze_pdf_text,
    classify_file,
    force_ocr_classification,
    is_garbled_word,
    text_quality,
    prepare_image_input,
    process_batch,
    run_ocr,
    scan_folder,
)


class ClassificationTests(unittest.TestCase):
    @patch("document_core.analyze_pdf_text", return_value=PdfTextReport(True, empty_pages=(1,)))
    def test_scanned_pdf_is_ocr_only_in_markdown_preflight(self, _report):
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

    def test_markitdown_text_formats_are_ready(self):
        for name in ("page.HTML", "page.htm", "book.epub", "notes.txt", "notes.md", "data.json", "data.xml", "mail.msg"):
            with self.subTest(name=name):
                result = classify_file(name)
                self.assertEqual((result.supported, result.needs_ocr, result.markdown_eligible), (True, False, True))

    def test_legacy_office_formats_need_their_office_app(self):
        for name, app in (("old.doc", "Word"), ("slides.PPT", "PowerPoint"), ("sheet.ods", "Excel")):
            for available in (True, False):
                with self.subTest(name=name, available=available), \
                        patch("document_core.office_app_available", return_value=available) as check:
                    result = classify_file(name)
                    check.assert_called_once_with(app)
                    self.assertTrue(result.supported)
                    self.assertFalse(result.needs_ocr)
                    self.assertEqual(result.markdown_eligible, available)
                    self.assertFalse(result.ocr_eligible)
                    self.assertEqual(result.reason, "" if available else f"Cần Microsoft {app} để đọc định dạng này.")

    def test_other_formats_are_not_supported(self):
        result = classify_file("archive.zip")

        self.assertFalse(result.supported)
        self.assertFalse(result.markdown_eligible)
        self.assertFalse(result.ocr_eligible)


class TextQualityTests(unittest.TestCase):
    def test_correct_text_in_several_languages_scores_one(self):
        for text in (
            "Ngày hiệu chuẩn: 15/03/2026. Thiết bị đạt yêu cầu kỹ thuật.",
            "Nhiệt độ 25°C ±0.5, độ dày 12 µm, ©2026 Công ty ABC®.",
            "Größe, façade, naïve, Ærø, São Paulo, Zürich, McDonald's, iPhone.",
        ):
            with self.subTest(text=text):
                self.assertEqual(text_quality(text)[0], 1)

    def test_garbled_words(self):
        for word in ("hi�u", "hiÿu", "Ngµy", "®¹t", "hiÖu", "chuÈn", "a\x9bb", "x", "6ҧQ", "6͙"):
            with self.subTest(word=word):
                self.assertTrue(is_garbled_word(word))
        for word in ("25°C", "µm", "ABC®", "«quoted»", "Ærø", "ĐẠT", "Ngày", "10kΩ", "Δt", "Москва", "ngày"):
            with self.subTest(word=word):
                self.assertFalse(is_garbled_word(word))

    def test_words_separated_by_control_characters(self):
        # Seen in real PDFs: pdfium returns U+0003 between words instead of a space.
        self.assertEqual(text_quality("Người\x03lập\x03biểu (Ký,\x03họ\x03tên) Kế\x03toán\x03trưởng"), (1.0, 9))

    def test_wrongly_encoded_text_scores_low(self):
        self.assertLess(text_quality("Biªn b¶n kiÓm tra thiÕt bÞ ®o l−êng")[0], 0.5)
        self.assertLess(text_quality("Ngay hi�u chudn: Thi�t b� �at")[0], 0.5)

    def test_too_little_text_is_not_judged(self):
        self.assertEqual(text_quality("Trang 3 / 12"), (None, 1))


@unittest.skipUnless(pdf_samples.ARIAL.exists(), "needs the Arial font")
class PdfTextReportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_correct_text_layer_needs_no_ocr(self):
        result = classify_file(pdf_samples.make_pdf(self.root / "good.pdf", "good"))
        self.assertEqual((result.needs_ocr, result.force_ocr, result.text_quality, result.reason), (False, False, 1.0, ""))

    def test_garbled_text_layers_need_forced_ocr_on_those_pages(self):
        for layers, pages in ((("tcvn3",), (1,)), (("broken",), (1,)), (("good", "tcvn3", "good"), (2,))):
            with self.subTest(layers=layers):
                result = classify_file(pdf_samples.make_pdf(self.root / "sample.pdf", *layers))
                self.assertEqual((result.needs_ocr, result.markdown_eligible, result.force_ocr), (True, False, True))
                self.assertEqual((result.ocr_pages, result.low_quality_pages), (pages, pages))
                self.assertEqual(result.reason, "PDF có chữ bị lỗi mã, cần OCR lại.")
                self.assertLess(result.text_quality, 0.8)

    def test_empty_pages_are_forced_together_with_garbled_ones(self):
        with patch("document_core.analyze_pdf_text", return_value=PdfTextReport(True, (3,), (1,), 0.2)):
            self.assertEqual(classify_file("x.pdf").ocr_pages, (1, 3))

    def test_unreadable_pdf(self):
        path = self.root / "bad.pdf"
        path.write_bytes(b"not a pdf")
        self.assertEqual(analyze_pdf_text(path), PdfTextReport(False))

    def test_force_ocr_classification_covers_every_page_of_pdfs_only(self):
        forced = force_ocr_classification(classify_file(pdf_samples.make_pdf(self.root / "good.pdf", "good")))
        self.assertEqual((forced.needs_ocr, forced.force_ocr, forced.ocr_pages, forced.markdown_eligible), (True, True, (), False))
        docx = FileClassification(self.root / "a.docx", True, False, True, False)
        self.assertIs(force_ocr_classification(docx), docx)


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

    def test_forced_pages_reach_ocr_only_for_forced_classifications(self):
        source = self.project_dir / "scan.pdf"
        cases = (
            (FileClassification(source, True, True, False, True), None),
            (FileClassification(source, True, True, False, True, force_ocr=True, ocr_pages=(2, 5)), (2, 5)),
            (FileClassification(source, True, True, False, True, force_ocr=True), ()),
        )
        for classification, expected in cases:
            with self.subTest(expected=expected), patch("document_core.run_ocr", return_value=self.ocr_dir / "scan.pdf") as run_ocr:
                process_batch(
                    [source], ProcessingMode.OCR, ["eng"], self.project_dir,
                    self.ocr_dir, self.markdown_dir, classifications=[classification]
                )
            self.assertEqual(run_ocr.call_args.args[6], expected)

    def test_run_ocr_replaces_text_only_when_forced(self):
        import ocrmypdf

        source = self.project_dir / "scan.pdf"
        for force_pages, expected in (
            (None, {"skip_text": True}),
            ((), {"force_ocr": True}),
            ((2, 5), {"force_ocr": True, "pages": "2,5"}),
        ):
            with self.subTest(force_pages=force_pages),                     patch.object(ocrmypdf, "ocr", return_value=ocrmypdf.ExitCode.ok) as ocr,                     patch("document_core.find_tesseract", return_value=None),                     patch("language_manager.prepare_managed_language_data", return_value=None):
                run_ocr(source, ["eng"], self.ocr_dir, self.project_dir, force_pages=force_pages)
            options = {key: ocr.call_args.kwargs.get(key) for key in ("skip_text", "force_ocr", "pages")}
            self.assertEqual({key: value for key, value in options.items() if value is not None}, expected)
            self.assertEqual(ocr.call_args.kwargs["pdfa_backend"], "internal")
            self.assertEqual(ocr.call_args.kwargs["rasterizer"], "pypdfium")

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


class OfficeBatchTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.markdown_dir = self.root / "markdown"
        self.work_dirs = []

    def fake_office(self, source, work_dir):
        self.work_dirs.append(work_dir)
        self.copied = source.read_bytes()
        converted = work_dir / f"{source.stem}.docx"
        converted.write_bytes(b"ooxml")
        return converted

    def run_batch(self, paths, mode=ProcessingMode.MARKDOWN, **kwargs):
        classifications = [FileClassification(path, True, False, True, False) for path in paths]
        return process_batch(
            paths, mode, [], self.root, self.root / "ocrpdf", self.markdown_dir,
            classifications=classifications, **kwargs
        )

    def test_office_file_is_converted_from_a_private_copy(self):
        source = self.root / "báo cáo.doc"
        source.write_bytes(b"legacy")
        with patch("document_core.convert_with_timeout", side_effect=self.fake_office), \
                patch("document_core.convert_markdown", return_value=self.markdown_dir / "báo cáo.md") as convert:
            result = self.run_batch([source])

        self.assertEqual(result.files[0].status, FileStatus.SUCCESS)
        self.assertEqual(self.copied, b"legacy")
        converted = convert.call_args.args[0]
        self.assertEqual(converted.name, "input.docx")
        self.assertNotEqual(converted.parent, source.parent)
        self.assertEqual(convert.call_args.kwargs["output_source"], source)
        self.assertFalse(self.work_dirs[0].exists())
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["báo cáo.doc", "markdown"])

    def test_office_copy_reads_the_opened_stream(self):
        source = self.root / "slides.ppt"

        @contextmanager
        def open_source(_path):
            yield io.BytesIO(b"from stream")

        with patch("document_core.convert_with_timeout", side_effect=self.fake_office), \
                patch("document_core.convert_markdown", return_value=self.markdown_dir / "slides.md"):
            result = self.run_batch([source], open_source=open_source)

        self.assertEqual(result.files[0].status, FileStatus.SUCCESS)
        self.assertEqual(self.copied, b"from stream")

    def test_failed_office_file_cleans_up_and_the_batch_continues(self):
        first, second = self.root / "hung.rtf", self.root / "next.odt"
        first.write_bytes(b"a")
        second.write_bytes(b"b")

        def converter(source, work_dir):
            self.work_dirs.append(work_dir)
            if len(self.work_dirs) == 1:
                raise TimeoutError("Microsoft Word did not respond in time.")
            return self.fake_office(source, work_dir)

        with patch("document_core.convert_with_timeout", side_effect=converter), \
                patch("document_core.convert_markdown", return_value=self.markdown_dir / "next.md"):
            result = self.run_batch([first, second])

        self.assertEqual(
            [(item.status, item.message) for item in result.files],
            [(FileStatus.FAILED, "Microsoft Word did not respond in time."), (FileStatus.SUCCESS, "")],
        )
        self.assertFalse(any(work_dir.exists() for work_dir in self.work_dirs))

    def test_ocr_only_mode_skips_office_files(self):
        source = self.root / "sheet.ods"
        classification = FileClassification(source, True, False, True, False)
        with patch("document_core.convert_with_timeout") as converter:
            result = process_batch(
                [source], ProcessingMode.OCR, [], self.root, self.root / "ocrpdf", self.markdown_dir,
                classifications=[classification]
            )

        converter.assert_not_called()
        self.assertEqual(
            (result.files[0].status, result.files[0].message),
            (FileStatus.SKIPPED, "Tệp không phù hợp với chế độ đã chọn."),
        )

    def test_progress_and_cancel_work_for_office_files(self):
        first, second = self.root / "a.doc", self.root / "b.doc"
        first.write_bytes(b"a")
        second.write_bytes(b"b")
        cancel = Event()
        updates = []

        def converter(source, work_dir):
            cancel.set()
            return self.fake_office(source, work_dir)

        with patch("document_core.convert_with_timeout", side_effect=converter), \
                patch("document_core.convert_markdown", return_value=self.markdown_dir / "a.md"):
            result = self.run_batch([first, second], cancel_event=cancel, progress_callback=updates.append)

        self.assertTrue(result.cancelled)
        self.assertEqual(len(result.files), 1)
        self.assertEqual([update.stage for update in updates], ["Markdown", "Hoàn tất"])


    def test_office_file_from_the_internet_is_skipped_before_office_opens_it(self):
        downloaded, local = self.root / "downloaded.doc", self.root / "local.doc"
        downloaded.write_bytes(b"a")
        local.write_bytes(b"b")
        with patch("document_core.from_internet_zone", side_effect=lambda path: path == downloaded) as zone, \
                patch("document_core.convert_with_timeout", side_effect=self.fake_office) as converter, \
                patch("document_core.convert_markdown", return_value=self.markdown_dir / "local.md"):
            result = self.run_batch([downloaded, local])

        self.assertEqual(
            (result.files[0].status, result.files[0].message),
            (FileStatus.SKIPPED, "Tệp tải từ Internet: không mở bằng Office để tránh tải tài nguyên từ xa."),
        )
        self.assertEqual(result.files[1].status, FileStatus.SUCCESS)
        self.assertEqual([call.args[0] for call in zone.call_args_list], [downloaded, local])
        converter.assert_called_once()

    def test_internet_zone_is_not_checked_for_other_formats(self):
        source = self.root / "report.docx"
        with patch("document_core.from_internet_zone") as zone, \
                patch("document_core.convert_markdown", return_value=self.markdown_dir / "report.md"):
            self.run_batch([source])
        zone.assert_not_called()


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "one" / "two").mkdir(parents=True)
        for name in ("a.pdf", "archive.zip", "~$a.docx", "one/b.docx", "one/two/c.xlsx"):
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
            ["hidden.pdf", "a.pdf", "archive.zip", "b.docx", "photo.png", "c.xlsx"],
        )

    @unittest.skipUnless(os.name == "nt", "hidden attribute is Windows-only")
    def test_hidden_and_system_files_are_not_listed(self):
        import ctypes

        for name, attribute in (("Thumbs.db", 0x2), ("desktop.ini", 0x4)):
            (self.root / name).write_bytes(b"x")
            ctypes.windll.kernel32.SetFileAttributesW(str(self.root / name), attribute)
        self.assertEqual(self.names(include_unsupported=True), ["a.pdf", "archive.zip"])

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