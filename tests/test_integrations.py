import base64
import contextlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from app_paths import build_app_paths
from document_core import (
    BatchResult,
    FileClassification,
    FileResult,
    FileStatus,
    collect_supported_files,
    has_subfolders,
)
import integration_service
from integration_service import ServiceError, convert, expand_inputs


def classification(path, supported=True, needs_ocr=False, reason=""):
    path = Path(path)
    return FileClassification(path, supported, needs_ocr, supported and not needs_ocr, needs_ocr, reason)


class TempTree(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        (self.root / "sub").mkdir()
        for name in ("a.pdf", "b.DOCX", "notes.txt", "~$b.docx", "sub/c.xlsx", "sub/d.png"):
            (self.root / name).write_bytes(b"x")
        self.paths = build_app_paths(self.root, self.root / "local")

    def tearDown(self):
        self._temp.cleanup()


class FolderCollectionTests(TempTree):
    def test_top_level_only_by_default_and_skips_unsupported_and_lock_files(self):
        found = collect_supported_files(self.root)
        self.assertEqual([path.name for path in found], ["a.pdf", "b.DOCX"])

    def test_recursive_includes_subfolders(self):
        found = collect_supported_files(self.root, recursive=True)
        self.assertEqual([path.name for path in found], ["a.pdf", "b.DOCX", "c.xlsx"])

    def test_has_subfolders(self):
        self.assertTrue(has_subfolders(self.root))
        self.assertFalse(has_subfolders(self.root / "sub"))

    def test_rejects_a_file(self):
        with self.assertRaises(NotADirectoryError):
            collect_supported_files(self.root / "a.pdf")


class ExpandInputsTests(TempTree):
    def test_mixes_files_and_folders_without_duplicates(self):
        found = expand_inputs([self.root / "a.pdf", self.root, self.root / "notes.txt"])
        self.assertEqual([path.name for path in found], ["a.pdf", "b.DOCX", "notes.txt"])

    def test_missing_paths_are_reported_together(self):
        with self.assertRaises(ServiceError) as raised:
            expand_inputs([self.root / "nope.pdf", self.root / "a.pdf"])
        self.assertEqual(raised.exception.code, "not_found")
        self.assertEqual(raised.exception.details["paths"], [str(self.root / "nope.pdf")])

    def test_folder_without_supported_files_is_an_error(self):
        images = self.root / "images"
        images.mkdir()
        (images / "photo.png").write_bytes(b"x")
        with self.assertRaises(ServiceError) as raised:
            expand_inputs([images], recursive=True)
        self.assertEqual(raised.exception.code, "no_input_files")


class ConvertTests(TempTree):
    def fake_batch(self, files, mode, languages, project_dir, ocr_dir, markdown_dir, **kwargs):
        markdown_dir.mkdir(parents=True, exist_ok=True)
        output = markdown_dir / "a_123.md"
        output.write_text("# Title", encoding="utf-8")
        self.batch_call = {"mode": mode, "languages": languages, "markdown_dir": markdown_dir}
        return BatchResult((
            FileResult(Path(files[0]), FileStatus.SUCCESS, "Markdown", (output,)),
            FileResult(Path(files[1]), FileStatus.SKIPPED, "Bỏ qua", message="Định dạng này chưa được hỗ trợ."),
            FileResult(Path(files[2]), FileStatus.FAILED, "Lỗi", message="boom"),
        ))

    def run_convert(self, **options):
        inputs = [self.root / "a.pdf", self.root / "notes.txt", self.root / "b.DOCX"]
        with patch.object(integration_service, "classify_file", side_effect=lambda path: classification(path)), \
                patch.object(integration_service, "process_batch", side_effect=self.fake_batch):
            return convert(inputs, self.paths, **options)

    def test_result_is_json_ready_with_stable_codes(self):
        result = self.run_convert(mode="markdown", include_content=True, output_dir=str(self.root / "out"))
        json.dumps(result)
        self.assertEqual(result["summary"], {"total": 3, "succeeded": 1, "skipped": 1, "failed": 1})
        success, skipped, failed = result["files"]
        self.assertEqual(success["markdown"], "# Title")
        self.assertEqual(skipped["stage"], "skipped")
        self.assertEqual(skipped["reason_code"], "unsupported_format")
        self.assertEqual(skipped["message"], "This file format is not supported.")
        self.assertEqual(failed["stage"], "error")
        self.assertEqual(failed["message"], "boom")
        self.assertEqual(self.batch_call["markdown_dir"], self.root / "out" / "markdown")

    def test_invalid_mode(self):
        with self.assertRaises(ServiceError) as raised:
            self.run_convert(mode="pdf")
        self.assertEqual(raised.exception.code, "invalid_mode")

    def test_relative_output_dir_is_rejected(self):
        with self.assertRaises(ServiceError) as raised:
            self.run_convert(mode="markdown", output_dir="out")
        self.assertEqual(raised.exception.code, "invalid_output_dir")

    def test_ocr_requires_tesseract(self):
        with patch.object(integration_service, "find_tesseract", return_value=None):
            with self.assertRaises(ServiceError) as raised:
                self.run_convert(mode="ocr", languages=["eng"])
        self.assertEqual(raised.exception.code, "tesseract_missing")

    def test_ocr_reports_missing_languages(self):
        with patch.object(integration_service, "find_tesseract", return_value="tesseract"), \
                patch.object(integration_service, "get_available_languages", return_value=["eng", "osd"]):
            with self.assertRaises(ServiceError) as raised:
                self.run_convert(mode="ocr", languages=["eng", "vie"])
        self.assertEqual(raised.exception.code, "missing_ocr_languages")
        self.assertEqual(raised.exception.details["languages"], ["vie"])

    def test_markdown_mode_does_not_need_ocr_runtime(self):
        with patch.object(integration_service, "find_tesseract", return_value=None):
            result = self.run_convert(mode="markdown")
        self.assertEqual(result["languages"], [])


class CliTests(TempTree):
    def run_cli(self, *argv):
        from markitdown_cli import main

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("app_runtime.prepare_runtime", return_value=self.paths), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_convert_exit_code_reflects_failures(self):
        payload = {"summary": {"failed": 1}, "cancelled": False, "files": []}
        with patch("integration_service.convert", return_value=payload) as convert_mock:
            code, stdout, _ = self.run_cli("convert", str(self.root), "-r", "-l", "vie+eng", "-l", "fra", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout), payload)
        self.assertEqual(convert_mock.call_args.kwargs["languages"], ["vie", "eng", "fra"])
        self.assertTrue(convert_mock.call_args.kwargs["recursive"])

    def test_request_errors_exit_with_2_and_json_error(self):
        code, stdout, _ = self.run_cli("classify", str(self.root / "missing.pdf"), "--json")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["error"]["code"], "not_found")

    def test_classify_text_output(self):
        with patch.object(integration_service, "classify_file", side_effect=lambda path: classification(path)):
            code, stdout, _ = self.run_cli("classify", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("a.pdf", stdout)


class ApiTests(TempTree):
    def setUp(self):
        super().setUp()
        from api_server import create_server

        self.server = create_server(self.paths, "127.0.0.1", 0, token="secret")
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, method, path, body=None, token="secret"):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_health_is_public_but_other_routes_need_the_token(self):
        self.assertEqual(self.request("GET", "/v1/health", token=None)[0], 200)
        status, body = self.request("GET", "/v1/info", token="wrong")
        self.assertEqual((status, body["error"]["code"]), (401, "unauthorized"))
        self.assertEqual(self.request("GET", "/v1/info")[0], 200)

    def test_relative_paths_are_rejected(self):
        status, body = self.request("POST", "/v1/convert", {"paths": ["a.pdf"]})
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"))

    def test_missing_paths_are_404(self):
        status, body = self.request("POST", "/v1/classify", {"paths": [str(self.root / "nope.pdf")]})
        self.assertEqual((status, body["error"]["code"]), (404, "not_found"))

    def test_uploads_use_only_the_base_name_and_report_it(self):
        seen = {}

        def fake_convert(inputs, paths, **options):
            seen["inputs"] = [Path(item) for item in inputs]
            seen["content"] = seen["inputs"][0].read_bytes()
            return {"files": [{"source": str(seen["inputs"][0])}], "summary": {}}

        content = base64.b64encode(b"docx-bytes").decode()
        with patch("api_server.convert", side_effect=fake_convert):
            status, body = self.request("POST", "/v1/convert", {"files": [{"name": "..\\..\\evil.docx", "content_base64": content}]})
        self.assertEqual(status, 200)
        self.assertEqual(seen["inputs"][0].name, "evil.docx")
        self.assertEqual(seen["content"], b"docx-bytes")
        self.assertFalse(seen["inputs"][0].exists())
        self.assertEqual(body["files"][0], {"source": "evil.docx", "uploaded": True})

    def test_unsupported_upload_is_rejected(self):
        status, body = self.request("POST", "/v1/convert", {"files": [{"name": "x.exe", "content_base64": ""}]})
        self.assertEqual((status, body["error"]["code"]), (400, "unsupported_format"))

    def raw_request(self, path, data=b"{}", headers=None):
        request = urllib.request.Request(self.base + path, data=data, method="POST", headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_cross_site_simple_posts_are_rejected(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded", None):
            with self.subTest(content_type=content_type):
                headers = {"Authorization": "Bearer secret"}
                if content_type:
                    headers["Content-Type"] = content_type
                status, body = self.raw_request("/v1/classify", headers=headers)
                self.assertEqual((status, body["error"]["code"]), (415, "unsupported_media_type"))

    def test_json_with_charset_is_accepted(self):
        status, _ = self.raw_request(
            "/v1/classify",
            json.dumps({"paths": [str(self.root / "nope.pdf")]}).encode(),
            {"Authorization": "Bearer secret", "Content-Type": "application/json; charset=utf-8"},
        )
        self.assertEqual(status, 404)

    def test_rebound_host_names_are_rejected(self):
        for host in ("evil.example", "evil.example:8765", "127.0.0.1.evil.example"):
            with self.subTest(host=host):
                request = urllib.request.Request(self.base + "/v1/health", headers={"Host": host})
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(raised.exception.code, 403)
                self.assertEqual(json.loads(raised.exception.read())["error"]["code"], "invalid_host")

    def test_loopback_host_names_are_accepted(self):
        for host in ("localhost:8765", "127.0.0.1", "[::1]:8765"):
            with self.subTest(host=host):
                request = urllib.request.Request(self.base + "/v1/health", headers={"Host": host})
                with urllib.request.urlopen(request, timeout=10) as response:
                    self.assertEqual(response.status, 200)

    def test_unknown_route_and_wrong_method(self):
        self.assertEqual(self.request("GET", "/v1/nope")[0], 404)
        self.assertEqual(self.request("GET", "/v1/convert")[0], 405)


class ApiBindingTests(unittest.TestCase):
    def test_non_loopback_requires_token(self):
        from api_server import create_server, is_loopback, request_host

        self.assertEqual(request_host("[::1]:8765"), "::1")
        self.assertEqual(request_host("localhost:8765"), "localhost")
        self.assertEqual(request_host("::1"), "::1")
        self.assertTrue(is_loopback("127.0.0.1"))
        self.assertTrue(is_loopback("::1"))
        self.assertFalse(is_loopback("0.0.0.0"))
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ServiceError) as raised:
                create_server(build_app_paths(Path("."), Path(".")), "0.0.0.0", 0)
        self.assertEqual(raised.exception.code, "token_required")


class McpTests(TempTree):
    def test_registers_tools_and_maps_service_errors(self):
        import asyncio

        from mcp_server import build_server

        server = build_server(self.paths)
        tools = asyncio.run(server.list_tools())
        self.assertEqual(
            sorted(tool.name for tool in tools),
            ["classify_documents", "convert_documents", "get_tool_info", "list_ocr_languages"],
        )
        convert_tool = next(tool for tool in tools if tool.name == "convert_documents")
        self.assertEqual(convert_tool.input_schema["required"], ["paths_or_folders"])


if __name__ == "__main__":
    unittest.main()
