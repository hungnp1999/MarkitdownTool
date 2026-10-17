"""Command-line interface for MarkitdownTool.

Exit codes: 0 = every file succeeded or was skipped, 1 = at least one file failed
or the batch was cancelled, 2 = the request could not run (bad input, missing OCR).
"""

import argparse
import json
import sys
from threading import Event

from app_runtime import APP_VERSION
from document_core import ProcessingMode, ProgressUpdate


EXIT_OK = 0
EXIT_FILE_FAILURES = 1
EXIT_REQUEST_ERROR = 2


def _split_languages(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    return [code for value in values for code in value.replace(",", "+").split("+") if code]


def _print_json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _print_error(error, as_json: bool) -> None:
    if as_json:
        _print_json({"error": error.to_dict()})
    else:
        print(f"error: {error.message}", file=sys.stderr)
        for key, value in error.details.items():
            print(f"  {key}: {value}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="markitdowntool",
        description="Convert PDF, DOCX, PPTX, and XLSX documents to Markdown, with OCR for scanned PDFs.",
    )
    parser.add_argument("--version", action="version", version=f"MarkitdownTool {APP_VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    convert = commands.add_parser("convert", help="Process files or folders.")
    convert.add_argument("paths", nargs="+", help="Files or folders to process.")
    convert.add_argument(
        "-m", "--mode",
        choices=[mode.value for mode in ProcessingMode],
        default=ProcessingMode.OCR_MARKDOWN.value,
        help="ocr = searchable PDF only, markdown = no OCR, ocr_markdown = OCR scanned PDFs then convert (default).",
    )
    convert.add_argument("-l", "--lang", action="append", help="OCR languages, e.g. vie+eng. Defaults to the app settings.")
    convert.add_argument("-o", "--output-dir", help="Absolute folder for results (markdown/ and ocrpdf/ are created inside).")
    convert.add_argument("-r", "--recursive", action="store_true", help="Include files in subfolders of folder inputs.")
    convert.add_argument("--include-content", action="store_true", help="Embed the Markdown text in --json output.")
    convert.add_argument("--json", action="store_true", help="Print a machine-readable JSON result.")
    convert.add_argument("-q", "--quiet", action="store_true", help="Do not print progress to stderr.")

    classify = commands.add_parser("classify", help="Check which files are supported or need OCR.")
    classify.add_argument("paths", nargs="+", help="Files or folders to check.")
    classify.add_argument("-r", "--recursive", action="store_true", help="Include files in subfolders of folder inputs.")
    classify.add_argument("--json", action="store_true", help="Print a machine-readable JSON result.")

    languages = commands.add_parser("languages", help="List installed OCR languages.")
    languages.add_argument("--json", action="store_true", help="Print a machine-readable JSON result.")

    manifest = commands.add_parser("manifest", help="Print capabilities as JSON (formats, modes, OCR languages, engines).")
    manifest.add_argument("--json", action="store_true", help="Accepted for consistency; the manifest is always JSON.")

    info = commands.add_parser("info", help="Show version, OCR runtime, and data folders.")
    info.add_argument("--json", action="store_true", help="Print a machine-readable JSON result.")

    api = commands.add_parser("api", help="Run the local HTTP API server.")
    api.add_argument("--host", default="127.0.0.1", help="Address to bind (default 127.0.0.1).")
    api.add_argument("--port", type=int, default=8765, help="Port to listen on (default 8765).")
    api.add_argument("--token", help="Require this bearer token. Defaults to MARKITDOWNTOOL_API_TOKEN.")

    commands.add_parser("mcp", help="Run the MCP server over stdio for AI agents.")
    return parser


def _run_convert(args, paths) -> int:
    from integration_service import convert, stage_name

    def report(update: ProgressUpdate) -> None:
        stage = stage_name(update.stage)
        if stage != "done":
            print(f"[{update.completed}/{update.total}] {stage}: {update.path.name}", file=sys.stderr)

    cancel = Event()
    try:
        result = convert(
            args.paths,
            paths,
            mode=args.mode,
            languages=_split_languages(args.lang),
            output_dir=args.output_dir,
            recursive=args.recursive,
            include_content=args.include_content and args.json,
            cancel_event=cancel,
            progress_callback=None if args.quiet or args.json else report,
        )
    except KeyboardInterrupt:
        cancel.set()
        print("cancelled", file=sys.stderr)
        return EXIT_FILE_FAILURES

    if args.json:
        _print_json(result)
    else:
        for item in result["files"]:
            detail = ", ".join(item["outputs"]) or item["message"]
            print(f"{item['status'].upper():8} {item['source']} -> {detail}")
        summary = result["summary"]
        print(f"{summary['succeeded']} succeeded, {summary['skipped']} skipped, {summary['failed']} failed.")
    return EXIT_FILE_FAILURES if result["summary"]["failed"] or result["cancelled"] else EXIT_OK


def _run_classify(args) -> int:
    from integration_service import classify

    result = classify(args.paths, recursive=args.recursive)
    if args.json:
        _print_json(result)
    else:
        for item in result["files"]:
            state = "needs OCR" if item["needs_ocr"] else ("ready" if item["supported"] else "unsupported")
            print(f"{state:12} {item['path']}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)

    from app_runtime import prepare_runtime
    from integration_service import ServiceError, get_info, get_manifest, list_languages

    paths = prepare_runtime(args.command)

    if args.command == "api":
        from api_server import serve

        return serve(paths, host=args.host, port=args.port, token=args.token)
    if args.command == "mcp":
        from mcp_server import serve

        serve(paths)
        return EXIT_OK

    as_json = getattr(args, "json", False)
    try:
        if args.command == "convert":
            return _run_convert(args, paths)
        if args.command == "classify":
            return _run_classify(args)
        if args.command == "manifest":
            _print_json(get_manifest(paths))
            return EXIT_OK
        result = get_info(paths) if args.command == "info" else list_languages(paths)
    except ServiceError as error:
        _print_error(error, as_json)
        return EXIT_REQUEST_ERROR

    if as_json:
        _print_json(result)
    elif args.command == "info":
        for key, value in result.items():
            print(f"{key}: {', '.join(value) if isinstance(value, list) else value}")
    else:
        selected = set(result["selected"])
        for item in result["languages"]:
            print(f"{'*' if item['code'] in selected else ' '} {item['code']}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
