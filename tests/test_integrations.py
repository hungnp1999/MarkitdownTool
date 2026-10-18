import base64
import contextlib
import hashlib
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
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
from office_converter import OFFICE_EXTENSIONS

sys.path.insert(0, str(Path(__file__).parent))
import pdf_samples  # noqa: E402


def classification(path, supported=True, needs_ocr=False, reason=""):
    path = Path(path)
    return FileClassification(path, supported, needs_ocr, supported and not needs_ocr, needs_ocr, reason)


class TempTree(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        (self.root / "sub").mkdir()
        for name in ("a.pdf", "b.DOCX", "archive.zip", "~$b.docx", "sub/c.xlsx", "sub/d.png"):
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
        self.assertEqual([path.name for path in found], ["a.pdf", "b.DOCX", "c.xlsx", "d.png"])

    def test_has_subfolders(self):
        self.assertTrue(has_subfolders(self.root))
        self.assertFalse(has_subfolders(self.root / "sub"))

    def test_rejects_a_file(self):
        with self.assertRaises(NotADirectoryError):
            collect_supported_files(self.root / "a.pdf")


class ExpandInputsTests(TempTree):
    def test_mixes_files_and_folders_without_duplicates(self):
        found = expand_inputs([self.root / "a.pdf", self.root, self.root / "archive.zip"])
        self.assertEqual([path.name for path in found], ["a.pdf", "archive.zip", "b.DOCX"])

    def test_missing_paths_are_reported_together(self):
        with self.assertRaises(ServiceError) as raised:
            expand_inputs([self.root / "nope.pdf", self.root / "a.pdf"])
        self.assertEqual(raised.exception.code, "not_found")
        self.assertEqual(raised.exception.details["paths"], [str(self.root / "nope.pdf")])

    def test_folder_with_only_unsupported_files_lists_them(self):
        images = self.root / "images"
        images.mkdir()
        (images / "photo.png").write_bytes(b"x")
        self.assertEqual([path.name for path in expand_inputs([images], recursive=True)], ["photo.png"])

    def test_empty_folder_is_an_error(self):
        (self.root / "empty").mkdir()
        with self.assertRaises(ServiceError) as raised:
            expand_inputs([self.root / "empty"], recursive=True)
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
        self.assertEqual([path.name for path in expand_inputs([self.root / "sub"], roots=roots)], ["c.xlsx", "d.png"])
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
        roots, source = integration_service.allowed_roots_with_source({}, {})
        self.assertTrue(roots)
        self.assertEqual(source, "local_drives")
        self.assertFalse(any(root.startswith(("\\\\", "//")) for root in roots))

    def test_configured_allowed_folders_are_used_when_the_variable_is_not_set(self):
        config = {"allowed_folders": [str(self.root / "sub"), "relative\\folder", "", 7]}
        roots, source = integration_service.allowed_roots_with_source({}, config)
        self.assertEqual((roots, source), ([os.path.realpath(self.root / "sub")], "config"))
        self.assertEqual([path.name for path in expand_inputs([self.root / "sub"], roots=roots)], ["c.xlsx", "d.png"])
        with self.assertRaises(ServiceError):
            expand_inputs([self.root / "a.pdf"], roots=roots)

    def test_variable_overrides_configured_folders(self):
        environment = {integration_service.ALLOWED_ROOTS_ENV: str(self.root)}
        roots, source = integration_service.allowed_roots_with_source(environment, {"allowed_folders": [str(self.root / "sub")]})
        self.assertEqual((roots, source), ([os.path.realpath(self.root)], "environment"))

    def test_only_relative_or_invalid_folders_fall_back_to_local_drives(self):
        for folders in ([], ["relative"], "D:\\Docs", None):
            with self.subTest(folders=folders):
                self.assertEqual(integration_service.allowed_roots_with_source({}, {"allowed_folders": folders})[1], "local_drives")

    def test_info_reports_effective_roots(self):
        with patch.object(integration_service, "read_config", return_value={"allowed_folders": [str(self.root)]}), \
                patch.dict("os.environ", {integration_service.ALLOWED_ROOTS_ENV: ""}):
            info = integration_service.get_info(self.paths)
        self.assertEqual((info["allowed_roots"], info["allowed_roots_source"]), ([os.path.realpath(self.root)], "config"))

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
        self.assertEqual(len(expand_inputs([self.root], limits=self.limits(max_files=3))), 3)
        for inputs in ([self.root], [self.root / "a.pdf", self.root / "b.DOCX", self.root / "archive.zip"]):
            with self.subTest(inputs=inputs), self.assertRaises(ServiceError) as raised:
                expand_inputs(inputs, recursive=True, limits=self.limits(max_files=2))
            self.assertEqual(raised.exception.code, "limit_exceeded")
            self.assertEqual(raised.exception.details, {"limit": "max_files", "value": 2})

    def test_deep_folders_are_a_limit_error(self):
        self.assertIn("c.xlsx", [path.name for path in expand_inputs([self.root], recursive=True, limits=self.limits(max_scan_depth=1))])
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

    def start_server(self, **options):
        from api_server import create_server

        with patch.dict("os.environ", {}, clear=True):
            server = create_server(self.paths, "127.0.0.1", 0, **options)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def status(self, url, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def test_server_without_explicit_token_still_requires_one(self):
        from app_runtime import load_or_create_api_token

        token_file = self.root / "settings" / "api_token"
        base = self.start_server(token_file=token_file)
        token = load_or_create_api_token(token_file)
        self.assertEqual(self.status(base + "/v1/info"), 401)
        self.assertEqual(self.status(base + "/v1/info", token), 200)

    def test_rotated_token_takes_effect_without_a_restart(self):
        from app_runtime import load_or_create_api_token, rotate_api_token

        token_file = self.root / "settings" / "api_token"
        base = self.start_server(token_file=token_file)
        old = load_or_create_api_token(token_file)
        self.assertEqual(self.status(base + "/v1/info", old), 200)
        new = rotate_api_token(token_file)
        self.assertNotEqual(new, old)
        self.assertEqual(self.status(base + "/v1/info", old), 401)
        self.assertEqual(self.status(base + "/v1/info", new), 200)

    def test_explicit_token_ignores_the_token_file(self):
        from app_runtime import rotate_api_token

        token_file = self.root / "settings" / "api_token"
        base = self.start_server(token="explicit", token_file=token_file)
        generated = rotate_api_token(token_file)
        self.assertEqual(self.status(base + "/v1/info", "explicit"), 200)
        self.assertEqual(self.status(base + "/v1/info", generated), 401)

    def test_rotation_writes_a_new_restricted_file(self):
        from app_runtime import load_or_create_api_token, rotate_api_token

        token_file = self.root / "settings" / "api_token"
        first = load_or_create_api_token(token_file)
        second = rotate_api_token(token_file)
        self.assertNotEqual(first, second)
        self.assertEqual(token_file.read_text(encoding="utf-8"), second)
        self.assertEqual(list(token_file.parent.glob("*.tmp")), [])
        if os.name == "nt":
            import subprocess

            acl = subprocess.run(["icacls", str(token_file)], capture_output=True, text=True).stdout
            self.assertNotIn("(I)", acl)
            self.assertIn("NT AUTHORITY\\SYSTEM:(F)", acl)
            self.assertNotIn("BUILTIN\\", acl)

    def test_token_is_still_written_when_the_acl_cannot_be_set(self):
        from app_runtime import load_or_create_api_token

        token_file = self.root / "settings" / "api_token"
        with patch("app_runtime.restrict_to_current_user", return_value=False), self.assertLogs("app_runtime", "WARNING"):
            token = load_or_create_api_token(token_file)
        self.assertEqual(token_file.read_text(encoding="utf-8"), token)

    def test_cli_creates_and_rotates_the_token_without_printing_it(self):
        from markitdown_cli import main

        token_file = self.root / "settings" / "api_token"
        tokens, outputs = [], []
        for argv in (["api-token"], ["api-token", "--rotate", "--json"]):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("app_runtime.prepare_runtime", return_value=self.paths), patch("app_runtime.API_TOKEN_FILE", token_file), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(argv), 0)
            tokens.append(token_file.read_text(encoding="utf-8"))
            outputs.append((stdout.getvalue(), stderr.getvalue()))
        self.assertEqual(outputs[0][0].strip(), str(token_file))
        self.assertEqual(json.loads(outputs[1][0]), {"token_file": str(token_file), "rotated": True})
        self.assertNotEqual(tokens[0], tokens[1])
        for token in tokens:
            for stdout_text, stderr_text in outputs:
                self.assertNotIn(token, stdout_text + stderr_text)


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
        inputs = [self.root / "a.pdf", self.root / "archive.zip", self.root / "b.DOCX"]
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

    def test_office_upload_is_rejected(self):
        status, body = self.request("POST", "/v1/convert", {"files": [{"name": "old.doc", "content_base64": ""}]})
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

    def test_manifest_endpoints_all_exist(self):
        status, manifest = self.request("GET", "/v1/manifest")
        self.assertEqual(status, 200)
        for endpoint in manifest["interfaces"]["api"]["endpoints"]:
            method, path = endpoint.split()
            with self.subTest(endpoint=endpoint):
                status, body = self.request(method, path, {} if method == "POST" else None)
                self.assertNotIn(body.get("error", {}).get("code"), ("not_found", "method_not_allowed"))

    def test_requests_are_audited_without_paths_or_tokens(self):
        with self.assertLogs("markitdowntool.audit", "INFO") as captured:
            self.request("POST", "/v1/classify", {"paths": [str(self.root)]})
            self.request("GET", "/v1/info", token="wrong")
            self.request("GET", "/v1/health", token=None)
        classify_line, unauthorized_line = captured.output
        self.assertIn("client=127.0.0.1 method=POST endpoint=/v1/classify status=200 files=3", classify_line)
        self.assertIn("endpoint=/v1/info status=401 files=-", unauthorized_line)
        for line in captured.output:
            self.assertNotIn(str(self.root), line)
            self.assertNotIn("secret", line)
            self.assertNotIn("wrong", line)

    def test_limit_errors_are_413(self):
        limits = {**integration_service.DEFAULT_LIMITS, "max_files": 1}
        with patch.object(integration_service, "integration_limits", return_value=limits):
            status, body = self.request("POST", "/v1/classify", {"paths": [str(self.root)]})
        self.assertEqual((status, body["error"]["code"]), (413, "limit_exceeded"))

    def test_unknown_route_and_wrong_method(self):
        self.assertEqual(self.request("GET", "/v1/nope")[0], 404)
        self.assertEqual(self.request("GET", "/v1/convert")[0], 405)


class OfficeGuiOnlyTests(TempTree):
    """ADR-011: Office can reach the network while it opens a file, so integrations never use it."""

    def setUp(self):
        super().setUp()
        office = patch("document_core.office_app_available", return_value=True)
        office.start()
        self.addCleanup(office.stop)
        self.source = self.root / "old.doc"
        self.source.write_bytes(b"x")

    def test_classify_refuses_office_formats_even_when_office_is_installed(self):
        item = integration_service.classify([self.source])["files"][0]
        self.assertFalse(item["supported"])
        self.assertFalse(item["markdown_eligible"])
        self.assertEqual(item["reason_code"], "office_gui_only")

    def test_convert_skips_office_formats(self):
        with patch("document_core.convert_markdown") as markdown:
            result = convert([self.source], self.paths, mode="markdown", output_dir=str(self.root / "out"))
        item = result["files"][0]
        self.assertEqual(item["status"], "skipped")
        self.assertEqual(item["reason_code"], "office_gui_only")
        markdown.assert_not_called()

    def test_manifest_marks_office_formats_app_only_and_info_leaves_them_out(self):
        with patch("integration_service.office_app_available", side_effect=lambda app: app != "Excel"):
            manifest = integration_service.get_manifest(self.paths)
        formats = {item["extension"]: item for item in manifest["formats"]}
        info = integration_service.get_info(self.paths)
        for extension, app in OFFICE_EXTENSIONS.items():
            with self.subTest(extension=extension):
                self.assertEqual((formats[extension]["interfaces"], formats[extension]["requires"]), (["gui"], app))
                self.assertNotIn(extension, info["supported_extensions"])
        self.assertEqual(formats[".docx"]["interfaces"], ["gui", "cli", "api", "mcp"])
        self.assertNotIn("requires", formats[".docx"])
        self.assertEqual(manifest["engines"]["office"], {"Excel": False, "PowerPoint": True, "Word": True})

    def test_needs_office_reason_codes(self):
        for app in ("Word", "PowerPoint", "Excel"):
            with self.subTest(app=app):
                code, message = integration_service._reason(f"Cần Microsoft {app} để đọc định dạng này.")
                self.assertEqual(code, "needs_office")
                self.assertIn(f"Microsoft {app}", message)


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


class ContractTests(TempTree):
    """Outputs must match the published JSON Schemas in schemas/."""

    def validate(self, name, data):
        import jsonschema

        schema = json.loads((Path(__file__).parent.parent / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(json.loads(json.dumps(data)), schema, cls=jsonschema.Draft202012Validator)

    def test_manifest_and_info_match_their_schemas(self):
        manifest = integration_service.get_manifest(self.paths)
        self.validate("manifest", manifest)
        self.validate("info", integration_service.get_info(self.paths))
        self.assertEqual(set(manifest["schemas"]), set(integration_service.SCHEMA_NAMES))
        kinds = {item["extension"]: (item["kind"], item["ocr"]) for item in manifest["formats"]}
        self.assertEqual((kinds[".xls"], kinds[".csv"], kinds[".png"]), (("document", False), ("document", False), ("image", True)))

    def test_classify_lists_unsupported_files_with_digests(self):
        result = integration_service.classify([self.root], recursive=True)
        self.validate("classify", result)
        by_name = {Path(item["path"]).name: item for item in result["files"]}
        self.assertEqual(sorted(by_name), ["a.pdf", "archive.zip", "b.DOCX", "c.xlsx", "d.png"])
        self.assertEqual((by_name["archive.zip"]["supported"], by_name["archive.zip"]["reason_code"]), (False, "unsupported_format"))
        self.assertEqual((by_name["d.png"]["needs_ocr"], by_name["d.png"]["reason_code"]), (True, "needs_ocr"))
        self.assertEqual(by_name["archive.zip"]["size"], 1)
        self.assertEqual(by_name["archive.zip"]["sha256"], hashlib.sha256(b"x").hexdigest())

    def test_convert_reports_unsupported_files_as_skipped_with_digests(self):
        def fake_batch(files, *args, open_source=None, **kwargs):
            with open_source(files[0]) as stream:
                stream.read()
            return BatchResult(tuple(
                FileResult(path, FileStatus.SUCCESS, "Markdown") if index == 0
                else FileResult(path, FileStatus.SKIPPED, "Bỏ qua", message="Định dạng này chưa được hỗ trợ.")
                for index, path in enumerate(files)
            ))

        with patch.object(integration_service, "process_batch", side_effect=fake_batch):
            result = convert([self.root / "sub"], self.paths, mode="markdown")
        self.validate("convert", result)
        converted, skipped = result["files"]
        self.assertEqual(Path(skipped["source"]).name, "d.png")
        self.assertEqual(skipped["reason_code"], "unsupported_format")
        for item in (converted, skipped):
            self.assertEqual((item["size"], item["sha256"]), (1, hashlib.sha256(b"x").hexdigest()))

    def test_unreadable_source_has_null_digest(self):
        self.assertEqual(integration_service.source_digest(self.root / "gone.pdf", integration_service.allowed_roots()), {"size": None, "sha256": None})

    def test_errors_match_their_schema(self):
        self.validate("error", {"error": ServiceError("limit_exceeded", "Too many.", {"limit": "max_files"}).to_dict()})

    def test_cli_manifest_prints_json(self):
        from markitdown_cli import main

        stdout = io.StringIO()
        with patch("app_runtime.prepare_runtime", return_value=self.paths), contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["manifest"]), 0)
        self.validate("manifest", json.loads(stdout.getvalue()))


class FormatConversionTests(TempTree):
    """Real conversions without mocks, for the formats added on top of MarkItDown's defaults."""

    def convert_one(self, path, mode="markdown", **options):
        return convert([path], self.paths, mode=mode, include_content=True, output_dir=str(self.root / "out"), **options)["files"][0]

    def test_legacy_excel_keeps_vietnamese_text(self):
        source = self.root / "sample.xls"
        shutil.copyfile(Path(__file__).parent / "fixtures" / "sample.xls", source)
        item = self.convert_one(source)
        self.assertEqual(item["status"], "success")
        self.assertIn("| TB-01 | Cân điện tử | 2026-03-15 |", item["markdown"])

    def test_csv_in_common_encodings(self):
        for encoding in ("utf-8", "utf-8-sig", "utf-16"):
            with self.subTest(encoding=encoding):
                source = self.root / f"devices-{encoding}.csv"
                source.write_bytes("Mã,Tên thiết bị\nTB-01,Cân điện tử\n".encode(encoding))
                item = self.convert_one(source)
                self.assertEqual(item["status"], "success")
                self.assertIn("| TB-01 | Cân điện tử |", item["markdown"])

    def test_html_table_keeps_vietnamese_text(self):
        source = self.root / "devices.html"
        source.write_text(
            '<html><head><meta charset="utf-8"></head><body><table>'
            "<tr><th>Mã</th><th>Tên thiết bị</th></tr><tr><td>TB-01</td><td>Cân điện tử</td></tr>"
            "</table></body></html>",
            encoding="utf-8",
        )
        item = self.convert_one(source)
        self.assertEqual(item["status"], "success")
        self.assertIn("| TB-01 | Cân điện tử |", item["markdown"])

    def test_html_does_not_fetch_remote_images(self):
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            source = self.root / "remote.html"
            source.write_text(
                f'<html><body><p>Cân điện tử</p><img src="http://127.0.0.1:{server.server_port}/x.png"></body></html>',
                encoding="utf-8",
            )
            item = self.convert_one(source)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(item["status"], "success")
        self.assertEqual(requests, [])

    def test_epub_keeps_vietnamese_text(self):
        source = self.root / "book.epub"
        with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as epub:
            epub.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            epub.writestr(
                "META-INF/container.xml",
                '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>'
                "</container>",
            )
            epub.writestr(
                "content.opf",
                '<?xml version="1.0" encoding="utf-8"?>'
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
                '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                '<dc:identifier id="id">test-book</dc:identifier><dc:title>Thiết bị</dc:title><dc:language>vi</dc:language>'
                "</metadata>"
                '<manifest><item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/></manifest>'
                '<spine><itemref idref="c1"/></spine></package>',
            )
            epub.writestr(
                "chapter1.xhtml",
                '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                "<head><title>Chương 1</title></head><body><p>Cân điện tử</p></body></html>",
            )
        item = self.convert_one(source)
        self.assertEqual(item["status"], "success")
        self.assertIn("Cân điện tử", item["markdown"])

    def test_plain_text_formats(self):
        contents = {
            ".txt": "Cân điện tử\n",
            ".md": "# Thiết bị\n\nCân điện tử\n",
            ".json": '{"ten": "Cân điện tử"}\n',
        }
        for extension, text in contents.items():
            with self.subTest(extension=extension):
                source = self.root / f"devices{extension}"
                source.write_text(text, encoding="utf-8")
                item = self.convert_one(source)
                self.assertEqual(item["status"], "success")
                self.assertIn("Cân điện tử", item["markdown"])

    def test_images_are_skipped_without_ocr(self):
        item = self.convert_one(self.root / "sub" / "d.png")
        self.assertEqual((item["status"], item["reason_code"]), ("skipped", "needs_ocr"))
        self.assertEqual(item["message"], "This image needs OCR before Markdown conversion.")

    @unittest.skipUnless(integration_service.find_tesseract(integration_service.APP_DIR), "needs Tesseract")
    def test_transparent_image_without_resolution_is_ocred(self):
        from PIL import Image, ImageDraw, ImageFont

        source = self.root / "photo.png"
        image = Image.new("RGBA", (1400, 400), (255, 255, 255, 0))
        ImageDraw.Draw(image).text((60, 140), "INVOICE 2026", fill="black", font=ImageFont.load_default(size=90))
        image.save(source)
        with patch.object(integration_service, "read_config", return_value={"ocr_languages": ["eng"]}):
            item = self.convert_one(source, mode="ocr_markdown", languages=["eng"])
        self.assertEqual((item["status"], item["stage"]), ("success", "ocr_markdown"), item["message"])
        self.assertIn("INVOICE 2026", item["markdown"])


@unittest.skipUnless(pdf_samples.ARIAL.exists(), "needs the Arial font")
class GarbledPdfTests(TempTree):
    def test_classify_reports_quality_and_reason(self):
        source = pdf_samples.make_pdf(self.root / "calibration.pdf", "good", "tcvn3")
        item = integration_service.classify([source])["files"][0]
        self.assertEqual((item["reason_code"], item["low_quality_pages"], item["needs_ocr"]), ("garbled_text", [2], True))
        self.assertAlmostEqual(item["text_quality"], 0.55)

    def test_markdown_mode_skips_garbled_pdfs(self):
        source = pdf_samples.make_pdf(self.root / "calibration.pdf", "tcvn3")
        result = convert([source], self.paths, mode="markdown")
        item = result["files"][0]
        self.assertEqual((item["status"], item["reason_code"], item["text_quality"]), ("skipped", "garbled_text", 0.1))
        self.assertFalse(result["force_ocr"])

    def test_force_ocr_marks_every_pdf(self):
        source = pdf_samples.make_pdf(self.root / "good.pdf", "good")
        seen = {}

        def fake_batch(files, *args, classifications=(), **kwargs):
            seen["classifications"] = classifications
            return BatchResult(())

        with patch.object(integration_service, "process_batch", side_effect=fake_batch),                 patch.object(integration_service, "_check_ocr_ready"):
            result = convert([source, self.root / "b.DOCX"], self.paths, mode="ocr_markdown", force_ocr=True)
        pdf, docx = seen["classifications"]
        self.assertEqual((pdf.force_ocr, pdf.ocr_pages, docx.force_ocr), (True, (), False))
        self.assertTrue(result["force_ocr"])

    @unittest.skipUnless(
        integration_service.find_tesseract(integration_service.APP_DIR)
        and "vie" in integration_service.get_available_languages(integration_service.APP_DIR),
        "needs Tesseract with Vietnamese",
    )
    def test_ocr_recovers_vietnamese_from_garbled_pages_and_keeps_good_ones(self):
        source = pdf_samples.make_pdf(self.root / "calibration.pdf", "good", "tcvn3")
        item = convert([source], self.paths, mode="ocr_markdown", languages=["vie"], include_content=True,
                       output_dir=str(self.root / "out"))["files"][0]
        self.assertEqual(item["status"], "success", item["message"])
        self.assertEqual(item["markdown"].count("Ngày hiệu chuẩn"), 2, item["markdown"])
        self.assertNotIn("Ngµy", item["markdown"])


class McpTests(TempTree):
    def test_registers_tools_and_maps_service_errors(self):
        import asyncio

        from mcp_server import build_server

        server = build_server(self.paths)
        tools = asyncio.run(server.list_tools())
        self.assertEqual(
            sorted(tool.name for tool in tools),
            sorted(integration_service.MCP_TOOLS),
        )
        convert_tool = next(tool for tool in tools if tool.name == "convert_documents")
        self.assertEqual(convert_tool.input_schema["required"], ["paths_or_folders"])


if __name__ == "__main__":
    unittest.main()
