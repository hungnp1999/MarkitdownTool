"""MCP server (stdio) exposing MarkitdownTool to AI agents and automation hosts."""

import contextlib
import sys
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app_paths import AppPaths
from app_runtime import APP_VERSION
from integration_service import ServiceError, allowed_roots, classify, convert, get_info, get_manifest, list_languages


INSTRUCTIONS = (
    "MarkitdownTool converts local PDF, DOCX, PPTX, and XLSX files to Markdown and can OCR scanned PDFs "
    "with Tesseract. Paths must be absolute paths on local drives of the machine running this server; network shares are refused. Use classify_documents "
    "to check files first, convert_documents to process them, list_ocr_languages before choosing OCR languages, "
    "and get_manifest to check capabilities and versions."
)


def build_server(paths: AppPaths) -> MCPServer:
    server = MCPServer(name="MarkitdownTool", version=APP_VERSION, instructions=INSTRUCTIONS)
    # Agents can be steered by prompt injection, so every path is confined to local allowed roots.
    roots = allowed_roots()

    def call(function, *args, **kwargs) -> dict[str, Any]:
        # Third-party converters may print; stdout belongs to the MCP protocol stream.
        with contextlib.redirect_stdout(sys.stderr):
            try:
                return function(*args, **kwargs)
            except ServiceError as error:
                details = f" {error.details}" if error.details else ""
                raise ToolError(f"{error.code}: {error.message}{details}") from error

    @server.tool(structured_output=True)
    def convert_documents(
        paths_or_folders: list[str],
        mode: Literal["ocr", "markdown", "ocr_markdown"] = "ocr_markdown",
        languages: list[str] | None = None,
        output_dir: str | None = None,
        recursive: bool = False,
        include_content: bool = True,
    ) -> dict[str, Any]:
        """Convert documents to Markdown and/or searchable PDF.

        mode: "markdown" converts without OCR, "ocr" only makes searchable PDFs from scanned PDFs,
        "ocr_markdown" OCRs scanned PDFs first and then converts everything to Markdown.
        languages: Tesseract codes such as ["vie", "eng"]; defaults to the app settings.
        output_dir: absolute folder for results; defaults to the app's output folder.
        recursive: include subfolders when a folder is given.
        include_content: return the Markdown text of each file in the result.
        """
        return call(
            convert,
            paths_or_folders,
            paths,
            mode=mode,
            languages=languages,
            output_dir=output_dir,
            recursive=recursive,
            include_content=include_content,
            roots=roots,
        )

    @server.tool(structured_output=True)
    def classify_documents(paths_or_folders: list[str], recursive: bool = False) -> dict[str, Any]:
        """Report whether each file is supported, needs OCR, and which modes can process it."""
        return call(classify, paths_or_folders, recursive=recursive, roots=roots)

    @server.tool(structured_output=True)
    def list_ocr_languages() -> dict[str, Any]:
        """List installed Tesseract OCR language codes and the languages selected in the app."""
        return call(list_languages, paths)

    @server.tool(structured_output=True)
    def get_tool_info() -> dict[str, Any]:
        """Show the MarkitdownTool version, supported formats, OCR runtime, and output folders."""
        return call(get_info, paths)

    @server.tool(name="get_manifest", structured_output=True)
    def manifest_tool() -> dict[str, Any]:
        """Describe capabilities: interfaces, supported formats, modes, OCR languages, engine versions, limits, schemas."""
        return call(get_manifest, paths)

    return server


def serve(paths: AppPaths) -> None:
    build_server(paths).run("stdio")
