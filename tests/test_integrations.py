import base64
import contextlib
import io
import json
import os
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


class PathPolicyTests(TempTree):
    def roots(self, *folders):
        return integration_service.allowed_roots({integration_service.ALLOWED_ROOTS_ENV: os.pathsep.join(map(str, folders))})

    def test_network_and_device_paths_are_refused_before_any_file_access(self):
        roots = integration_service.allowed_roots()
        for value in (r"\\evil\share\a.pdf", r"\\evil@SSL\DavWWWRoot\a.pdf", "//evil/share/a.pdf", r"\\?\UNC\evil\share", r"\\.\C:\a.pdf"):
            with self.subTest(value=value), patch("os.path.realpath") as realpath, patch.object(Path, "is_dir") as is_dir:
                with self.assertRaises(ServiceError) as raised:
                    expand_inputs([value], roots=roots)
                self.assertEqual(raised.exception.code, "path_not_allowed")
                realpath.assert_not_called()
                is_dir.assert_not_called()

    def test_configured_roots_narrow_access_without_prefix_confusion(self):
        roots = self.roots(self.root / "sub")
        self.assertEqual([path.name for path in expand_inputs([self.root / "sub"], roots=roots)], ["c.xlsx"])
        for value in (self.root / "a.pdf", str(self.root / "sub") + "2", self.root / "sub" / ".." / "a.pdf"):
            with self.subTest(value=value), self.assertRaises(ServiceError) as raised:
                expand_inputs([value], roots=roots)
            self.assertEqual(raised.exception.code, "path_not_allowed")

    @unittest.skipUnless(os.name == "nt", "junctions are Windows-only")
    def test_junction_that_leaves_the_allowed_root_is_refused(self):
        import _winapi

        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.docx").write_bytes(b"x")
        allowed = self.root / "allowed"
        allowed.mkdir()
        _winapi.CreateJunction(str(outside), str(allowed / "link"))
        roots = self.roots(allowed)
        for value in (allowed / "link", allowed / "link" / "secret.docx"):
            with self.subTest(value=value), self.assertRaises(ServiceError) as raised:
                expand_inputs([value], roots=roots)
            self.assertEqual(raised.exception.code, "path_not_allowed")
        with self.assertRaises(ServiceError):
            expand_inputs([allowed], recursive=True, roots=roots)

    def test_output_dir_must_be_inside_allowed_roots(self):
        roots = self.roots(self.root / "sub")
        for output_dir in (str(self.root / "elsewhere"), r"\\evil\share\out"):
            with self.subTest(output_dir=output_dir), self.assertRaises(ServiceError) as raised:
                convert([self.root / "sub" / "c.xlsx"], self.paths, mode="markdown", output_dir=output_dir, roots=roots)
            self.assertEqual(raised.exception.code, "path_not_allowed")

    def test_default_roots_are_local_drives_only(self):
        roots = integration_service.allowed_roots({})
        self.assertTrue(roots)
        self.assertFalse(any(root.startswith(("\\\\", "//")) for root in roots))

    def test_links_to_network_targets_are_refused_before_realpath(self):
        roots = self.roots(self.root)
        link = os.path.normcase(str(self.root / "sub"))
        for checker in ("islink", "isjunction"):
            for target in (r"\\?\UNC\evil\share", r"\\evil\share", r"\??\UNC\evil\share", r"\\.\pipe\x"):
                with self.subTest(checker=checker, target=target), \
                        patch(f"os.path.{checker}", side_effect=lambda path: path == link), \
                        patch("os.readlink", return_value=target), \
                        patch("os.path.realpath") as realpath:
                    with self.assertRaises(ServiceError) as raised:
                        integration_service.ensure_allowed_path(self.root / "sub" / "c.xlsx", roots)
                    self.assertEqual(raised.exception.code, "path_not_allowed")
                    realpath.assert_not_called()

    def test_links_to_local_targets_are_resolved_as_before(self):
        roots = self.roots(self.root)
        link = os.path.normcase(str(self.root / "sub"))
        for target in (r"\\?\C:\data", r"\??\Volume{1234}\data", "..\\data", "C:\\data"):
            with self.subTest(target=target), \
                    patch("os.path.isjunction", side_effect=lambda path: path == link), \
                    patch("os.readlink", return_value=target):
                self.assertEqual(integration_service.ensure_allowed_path(self.root / "sub" / "c.xlsx", roots).name, "c.xlsx")

    def test_open_verified_reads_the_checked_file(self):
        roots = self.roots(self.root)
        path = integration_service.ensure_allowed_path(self.root / "a.pdf", roots)
        with integration_service.open_verified(path, roots) as stream:
            self.assertEqual(stream.read(), b"x")

    @unittest.skipUnless(os.name == "nt", "junctions are Windows-only")
    def test_folder_swapped_for_a_junction_after_the_check_is_refused(self):
        import _winapi

        outside = self.root / "outside"
        outside.mkdir()
        (outside / "c.xlsx").write_bytes(b"secret")
        roots = self.roots(self.root / "sub")
        checked = integration_service.ensure_allowed_path(self.root / "sub" / "c.xlsx", roots)
        (self.root / "sub").rename(self.root / "moved")
        _winapi.CreateJunction(str(outside), str(self.root / "sub"))
        with self.assertRaises(ServiceError) as raised:
            with integration_service.open_verified(checked, roots):
                pass
        self.assertEqual(raised.exception.code, "path_not_allowed")


class LimitTests(TempTree):
    def limits(self, **overrides):
        return {**integration_service.DEFAULT_LIMITS, **overrides}

    def test_config_overrides_only_valid_positive_integers(self):
        limits = integration_service.integration_limits(
            {"integration_limits": {"max_files": 5, "max_scan_depth": 0, "max_content_bytes": "big", "batch_wait_seconds": True}}
        )
        self.assertEqual(limits, self.limits(max_files=5))
        self.assertEqual(integration_service.integration_limits({"integration_limits": []}), self.limits())

    def test_too_many_files_is_a_limit_error(self):
        self.assertEqual(len(expand_inputs([self.root], limits=self.limits(max_files=2))), 2)
        for inputs in ([self.root], [self.root / "a.pdf", self.root / "b.DOCX", self.root / "notes.txt"]):
            with self.subTest(inputs=inputs), self.assertRaises(ServiceError) as raised:
                expand_inputs(inputs, recursive=True, limits=self.limits(max_files=2))
            self.assertEqual(raised.exception.code, "limit_exceeded")
            self.assertEqual(raised.exception.details, {"limit": "max_files", "value": 2})

    def test_deep_folders_are_a_limit_error(self):
        self.assertEqual(expand_inputs([self.root], recursive=True, limits=self.limits(max_scan_depth=1))[-1].name, "c.xlsx")
        (self.root / "sub" / "deeper").mkdir()
        with self.assertRaises(ServiceError) as raised:
            expand_inputs([self.root], recursive=True, limits=self.limits(max_scan_depth=1))
        self.assertEqual(raised.exception.details, {"limit": "max_scan_depth", "value": 1})

    def test_info_reports_the_limits(self):
        self.assertEqual(integration_service.get_info(self.paths)["limits"], integration_service.integration_limits())


class ConvertLimitTests(TempTree):
    def fake_batch(self, files, mode, languages, project_dir, ocr_dir, markdown_dir, **kwargs):
        markdown_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for index, path in enumerate(files):
            output = markdown_dir / f"{index}.md"
            output.write_text("12345", encoding="utf-8")
            results.append(FileResult(Path(path), FileStatus.SUCCESS, "Markdown", (output,)))
        return BatchResult(tuple(results))

    def run_convert(self, limits, **options):
        inputs = [self.root / "a.pdf", self.root / "b.DOCX"]
        with patch.object(integration_service, "integration_limits", return_value={**integration_service.DEFAULT_LIMITS, **limits}), \
                patch.object(integration_service, "classify_file", side_effect=lambda path: classification(path)), \
                patch.object(integration_service, "process_batch", side_effect=self.fake_batch):
            return convert(inputs, self.paths, mode="markdown", **options)

    def test_content_beyond_the_budget_is_omitted(self):
        first, second = self.run_convert({"max_content_bytes": 8}, include_content=True)["files"]
        self.assertEqual(first["markdown"], "12345")
        self.assertNotIn("content_omitted", first)
        self.assertEqual((second["markdown"], second["content_omitted"]), (None, "limit_exceeded"))

    def test_waiting_for_a_running_batch_times_out_as_busy(self):
        with integration_service._batch_lock:
            with self.assertRaises(ServiceError) as raised:
                self.run_convert({"batch_wait_seconds": 0.01})
        self.assertEqual(raised.exception.code, "busy")

    def test_convert_opens_sources_through_the_verified_handle(self):
        captured = {}

        def recording_batch(files, *args, **kwargs):
            captured.update(kwargs)
            return self.fake_batch(files, *args, **kwargs)

        with patch.object(integration_service, "classify_file", side_effect=lambda path: classification(path)), \
                patch.object(integration_service, "process_batch", side_effect=recording_batch):
            convert([self.root / "a.pdf"], self.paths, mode="markdown")
        with captured["open_source"](self.root / "a.pdf") as stream:
            self.assertEqual(stream.read(), b"x")


class ApiTokenTests(TempTree):
    def test_token_is_generated_once_and_reused(self):
        from app_runtime import load_or_create_api_token

        token_file = self.root / "settings" / "api_token"
        first = load_or_create_api_token(token_file)
        self.assertGreaterEqual(len(first), 32)
        self.assertEqual(load_or_create_api_token(token_file), first)

    def test_server_without_explicit_token_still_requires_one(self):
        from api_server import create_server

        with patch.dict("os.environ", {}, clear=True), patch("api_server.load_or_create_api_token", return_value="generated"):
            server = create_server(self.paths, "127.0.0.1", 0)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(base + "/v1/info", timeout=10)
            self.assertEqual(raised.exception.code, 401)
            request = urllib.request.Request(base + "/v1/info", headers={"Authorization": "Bearer generated"})
            with urllib.request.urlopen(request, timeout=10) as response:
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()


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

    def test_network_paths_are_forbidden(self):
        status, body = self.request("POST", "/v1/classify", {"paths": ["\\\\evil\\share\\a.pdf"]})
        self.assertEqual((status, body["error"]["code"]), (403, "path_not_allowed"))

    def test_limit_errors_are_413(self):
        limits = {**integration_service.DEFAULT_LIMITS, "max_files": 1}
        with patch.object(integration_service, "integration_limits", return_value=limits):
            status, body = self.request("POST", "/v1/classify", {"paths": [str(self.root)]})
        self.assertEqual((status, body["error"]["code"]), (413, "limit_exceeded"))

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
