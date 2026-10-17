"""Local HTTP/JSON API for MarkitdownTool.

Endpoints (all JSON):
  GET  /v1/health       liveness check, no token needed
  GET  /v1/manifest     capabilities: interfaces, formats, OCR languages, engine versions
  GET  /v1/info         version, OCR runtime, data folders
  GET  /v1/languages    installed OCR languages
  POST /v1/classify     {"paths": [...], "recursive": false}
  POST /v1/convert      {"paths": [...] | "files": [{"name", "content_base64"}], "mode", "languages",
                         "output_dir", "recursive", "include_content"}

Every endpoint except /v1/health requires a bearer token. Without --token or
MARKITDOWNTOOL_API_TOKEN the server generates one in the per-user settings folder
(api_token), so other accounts on the machine cannot use the API. Binding to a
non-loopback address requires an explicit token. Paths are limited to the roots from
integration_service.allowed_roots (local drives by default, never UNC shares). On loopback the
server also rejects non-loopback Host headers (DNS rebinding) and POST bodies that are
not application/json (cross-site form or text/plain posts that skip CORS preflight).
"""

import base64
import binascii
import hmac
import ipaddress
import json
import logging
import os
import posixpath
import sys
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app_paths import AppPaths
from app_runtime import API_TOKEN_FILE, APP_VERSION, load_or_create_api_token
from document_core import SUPPORTED_EXTENSIONS
from integration_service import ServiceError, allowed_roots, classify, convert, get_info, get_manifest, list_languages


TOKEN_ENV = "MARKITDOWNTOOL_API_TOKEN"
MAX_BODY_BYTES = 200 * 1024 * 1024

_ERROR_STATUS = {
    "not_found": HTTPStatus.NOT_FOUND,
    "path_not_allowed": HTTPStatus.FORBIDDEN,
    "tesseract_missing": HTTPStatus.SERVICE_UNAVAILABLE,
    "languages_unavailable": HTTPStatus.SERVICE_UNAVAILABLE,
    "busy": HTTPStatus.SERVICE_UNAVAILABLE,
    "limit_exceeded": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
}


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def request_host(header: str) -> str:
    """Host name from a Host header, without the port; IPv6 brackets are removed."""
    header = header.strip()
    if header.startswith("["):
        return header[1:].split("]", 1)[0]
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


def _bool(body: dict, key: str) -> bool:
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise ServiceError("invalid_request", f"'{key}' must be true or false.")
    return value


def _string_list(body: dict, key: str) -> list[str]:
    value = body.get(key) or []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ServiceError("invalid_request", f"'{key}' must be a list of non-empty strings.")
    return value


def _absolute_paths(body: dict) -> list[str]:
    paths = _string_list(body, "paths")
    relative = [path for path in paths if not os.path.isabs(path)]
    if relative:
        raise ServiceError("invalid_request", "'paths' must be absolute paths on the server.", {"paths": relative})
    return paths


def _write_uploads(files: object, folder: Path) -> list[Path]:
    if not isinstance(files, list):
        raise ServiceError("invalid_request", "'files' must be a list of {name, content_base64} objects.")
    written = []
    for index, item in enumerate(files):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("content_base64"), str):
            raise ServiceError("invalid_request", f"files[{index}] needs string 'name' and 'content_base64'.")
        name = posixpath.basename(item["name"].replace("\\", "/"))
        if not name or os.path.splitext(name)[1].lower() not in SUPPORTED_EXTENSIONS:
            raise ServiceError("unsupported_format", f"files[{index}] must be one of: {', '.join(sorted(SUPPORTED_EXTENSIONS))}.")
        try:
            content = base64.b64decode(item["content_base64"], validate=True)
        except (binascii.Error, ValueError) as error:
            raise ServiceError("invalid_request", f"files[{index}].content_base64 is not valid base64.") from error
        # One subfolder per upload keeps identical names from overwriting each other.
        base = os.path.realpath(folder)
        normalized = os.path.normpath(os.path.join(base, str(index), name))
        if not normalized.startswith(base + os.sep):
            raise ServiceError("invalid_request", f"files[{index}].name is not a plain file name.")
        target = Path(normalized)
        target.parent.mkdir()
        target.write_bytes(content)
        written.append(target)
    return written


class ApiHandler(BaseHTTPRequestHandler):
    server_version = f"MarkitdownTool/{APP_VERSION}"
    app_paths: AppPaths
    token: str | None = None
    loopback_only = True
    roots: list[str] = []

    def log_message(self, format: str, *args) -> None:
        logging.getLogger(__name__).info("%s %s", self.address_string(), format % args)

    def _send(self, status: HTTPStatus, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, code: str, message: str, details: dict | None = None) -> None:
        self._send(status, {"error": ServiceError(code, message, details).to_dict()})

    def _authorized(self) -> bool:
        if not self.token:
            return True
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        return hmac.compare_digest(supplied.encode("utf-8"), self.token.encode("utf-8"))

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0:
            raise ServiceError("invalid_request", "Invalid Content-Length header.")
        if length > MAX_BODY_BYTES:
            raise ServiceError("payload_too_large", f"Request body exceeds {MAX_BODY_BYTES // (1024 * 1024)} MB.")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ServiceError("invalid_request", f"Request body is not valid JSON: {error}") from error
        if not isinstance(body, dict):
            raise ServiceError("invalid_request", "Request body must be a JSON object.")
        return body

    def _reject(self, status: HTTPStatus, code: str, message: str) -> None:
        """Answer an error before the body was read; drain it so the client is not reset mid-upload."""
        try:
            remaining = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            remaining = -1
        if 0 <= remaining <= MAX_BODY_BYTES:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                remaining -= len(chunk)
        else:
            self.close_connection = True
        self._send_error(status, code, message)

    def _dispatch(self, method: str) -> None:
        route = self.path.split("?", 1)[0].rstrip("/")
        if self.loopback_only and not is_loopback(request_host(self.headers.get("Host", ""))):
            # Blocks DNS rebinding: a web page served from another name must not reach the local API.
            self._reject(HTTPStatus.FORBIDDEN, "invalid_host", "The Host header must be a loopback address.")
            return
        if method == "POST" and self.headers.get_content_type() != "application/json":
            # Browsers send cross-site text/plain or form posts without a CORS preflight; JSON requires one.
            self._reject(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "unsupported_media_type", "Content-Type must be application/json.")
            return
        if method == "GET" and route == "/v1/health":
            self._send(HTTPStatus.OK, {"status": "ok", "version": APP_VERSION})
            return
        if not self._authorized():
            self._reject(HTTPStatus.UNAUTHORIZED, "unauthorized", "Missing or invalid bearer token.")
            return

        routes = {
            ("GET", "/v1/manifest"): lambda: get_manifest(self.app_paths),
            ("GET", "/v1/info"): lambda: get_info(self.app_paths),
            ("GET", "/v1/languages"): lambda: list_languages(self.app_paths),
            ("POST", "/v1/classify"): self._classify,
            ("POST", "/v1/convert"): self._convert,
        }
        handler = routes.get((method, route))
        if handler is None:
            known = {path for _method, path in routes} | {"/v1/health"}
            if route in known:
                self._reject(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", f"{method} is not allowed on {route}.")
            else:
                self._reject(HTTPStatus.NOT_FOUND, "not_found", f"Unknown endpoint {route or '/'}.")
            return

        try:
            self._send(HTTPStatus.OK, handler())
        except ServiceError as error:
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if error.code == "payload_too_large" else _ERROR_STATUS.get(error.code, HTTPStatus.BAD_REQUEST)
            self._send(status, {"error": error.to_dict()})
        except Exception as error:
            logging.getLogger(__name__).exception("API request failed: %s %s", method, route)
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", str(error))

    def _classify(self) -> dict:
        body = self._read_json()
        return classify(_absolute_paths(body), recursive=_bool(body, "recursive"), roots=self.roots)

    def _convert(self) -> dict:
        body = self._read_json()
        options = {
            "mode": body.get("mode", "ocr_markdown"),
            "languages": _string_list(body, "languages") or None,
            "output_dir": body.get("output_dir") or None,
            "recursive": _bool(body, "recursive"),
            "include_content": _bool(body, "include_content"),
            "roots": self.roots,
        }
        if options["output_dir"] is not None and not isinstance(options["output_dir"], str):
            raise ServiceError("invalid_request", "'output_dir' must be a string.")
        paths = _absolute_paths(body)
        if "files" not in body:
            if not paths:
                raise ServiceError("invalid_request", "Send 'paths' or 'files'.")
            return convert(paths, self.app_paths, **options)

        with tempfile.TemporaryDirectory(prefix="markitdowntool_upload_") as folder:
            uploads = _write_uploads(body["files"], Path(folder))
            # Uploaded copies live in a private temp folder that may sit outside the configured roots.
            upload_root = os.path.realpath(folder)
            options["roots"] = [*self.roots, upload_root]
            result = convert([*paths, *uploads], self.app_paths, **options)
        upload_sources = {os.path.realpath(path): path.name for path in uploads}
        for item in result["files"]:
            if item["source"] in upload_sources:
                item["source"] = upload_sources[item["source"]]
                item["uploaded"] = True
        return result

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")


def create_server(
    paths: AppPaths,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
    roots: list[str] | None = None,
) -> ThreadingHTTPServer:
    token = token or os.environ.get(TOKEN_ENV) or None
    if not is_loopback(host) and not token:
        raise ServiceError("token_required", f"Set --token or {TOKEN_ENV} before binding to a non-loopback address.")
    # Every request needs a token: other accounts and processes on this machine can reach loopback too.
    # The generated token lives in the per-user settings folder, which other Windows accounts cannot read.
    token = token or load_or_create_api_token()
    handler = type(
        "ConfiguredApiHandler",
        (ApiHandler,),
        {
            "app_paths": paths,
            "token": token,
            "loopback_only": is_loopback(host),
            "roots": allowed_roots() if roots is None else roots,
        },
    )
    return ThreadingHTTPServer((host, port), handler)


def serve(paths: AppPaths, host: str = "127.0.0.1", port: int = 8765, token: str | None = None) -> int:
    try:
        server = create_server(paths, host, port, token)
    except (ServiceError, OSError) as error:
        print(f"error: {getattr(error, 'message', error)}", file=sys.stderr)
        return 2
    bound_host, bound_port = server.server_address[:2]
    print(f"MarkitdownTool API {APP_VERSION} listening on http://{bound_host}:{bound_port}/v1 (Ctrl+C to stop)", file=sys.stderr)
    if not (token or os.environ.get(TOKEN_ENV)):
        print(f"Bearer token: read it from {API_TOKEN_FILE}", file=sys.stderr)
    logging.getLogger(__name__).info("API server started on %s:%s", bound_host, bound_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
