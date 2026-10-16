import json
import importlib.metadata
import logging
import os
import queue
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from app_paths import copy_data_preserving_existing, find_application_resource
from app_runtime import APP_DIR, APP_VERSION, GITHUB_REPOSITORY, IS_FROZEN, cli_command, prepare_runtime, read_config, save_config
from document_core import (
    BatchResult,
    FileClassification,
    FileStatus,
    ProcessingMode,
    ProgressUpdate,
    SUPPORTED_EXTENSIONS,
    classify_file,
    collect_supported_files,
    find_tesseract,
    get_available_languages,
    has_subfolders,
    process_batch,
)
from i18n import translate
from language_manager import (
    install_language,
    list_language_packages,
    uninstall_language,
)


GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
APP_REGISTRY_KEY = r"Software\MarkitdownTool"

APP_PATHS = prepare_runtime("process")
DATA_DIR = APP_PATHS.data_dir
OUTPUT_DIR = APP_PATHS.output_dir
MARKDOWN_DIR = APP_PATHS.markdown_dir
OCRPDF_DIR = APP_PATHS.ocr_pdf_dir


def load_config() -> dict:
    return read_config()


def save_data_directory_registry(path: Path) -> None:
    if os.name != "nt":
        return
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_REGISTRY_KEY) as key:
            winreg.SetValueEx(key, "DataDir", 0, winreg.REG_SZ, str(path))
    except OSError:
        logging.exception("Could not persist user data directory in HKCU")


def is_running_in_venv() -> bool:
    return sys.prefix != sys.base_prefix


def relaunch_in_venv() -> None:
    python_exe = APP_DIR / ".venv" / "Scripts" / "python.exe"
    if not python_exe.exists():
        raise FileNotFoundError(f"Không tìm thấy venv:\n{python_exe}")
    subprocess.run([str(python_exe), str(__file__)], check=True)
    raise SystemExit


class MarkitdownApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"MarkitdownTool {APP_VERSION}")
        self.root.geometry("1000x720")
        self.root.minsize(820, 600)
        self.root.configure(bg="#f2f5f4")

        self.config = load_config()
        self.ui_language = self.config.get("ui_language", "en")
        self.files: list[Path] = []
        self.classifications: tuple[FileClassification, ...] = ()
        self.mode = tk.StringVar(value=ProcessingMode.OCR_MARKDOWN.value)
        self.status = tk.StringVar(value=self.t("initial_status"))
        self.language_label = tk.StringVar(value="")
        self.events: queue.Queue = queue.Queue()
        self.cancel_event: threading.Event | None = None
        self.busy = False
        self.close_when_finished = False
        self.migrating_legacy_output = False
        self.languages = self.config.get("ocr_languages", ["vie", "eng"])
        self.file_rows: dict[str, Path] = {}
        self.mode_buttons: list[ttk.Radiobutton] = []

        self._configure_style()
        self._build_ui()
        self._refresh_language_label()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_events)

    def t(self, key: str, **values: object) -> str:
        return translate(key, self.ui_language, **values)

    def _translate_reason(self, reason: str) -> str:
        if reason == "PDF cần OCR trước khi tạo Markdown.":
            return self.t("scanned_needs_ocr")
        if reason == "Định dạng này chưa được hỗ trợ.":
            return self.t("skipped_format")
        return reason

    def _translate_stage(self, stage: str) -> str:
        if stage == "Bỏ qua":
            return self.t("skipped")
        if stage == "Lỗi":
            return self.t("error")
        if stage == "OCR + Markdown":
            return self.t("ocr_and_markdown")
        return stage

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f2f5f4")
        style.configure("Panel.TFrame", background="#ffffff")
        style.configure("TLabel", background="#f2f5f4", foreground="#263735", font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background="#ffffff", foreground="#263735", font=("Segoe UI", 10))
        style.configure("Title.TLabel", background="#123c3b", foreground="#ffffff", font=("Segoe UI Semibold", 19))
        style.configure("Subtitle.TLabel", background="#123c3b", foreground="#c5ded8", font=("Segoe UI", 9))
        style.configure("Section.TLabel", background="#ffffff", foreground="#123c3b", font=("Segoe UI Semibold", 11))
        style.configure("TButton", font=("Segoe UI", 9), padding=(10, 6))
        style.configure("Accent.TButton", background="#087e75", foreground="#ffffff", padding=(14, 8))
        style.map("Accent.TButton", background=[("active", "#066c65"), ("disabled", "#93aaa6")])
        style.configure("Treeview", rowheight=29, font=("Segoe UI", 9), background="#ffffff", fieldbackground="#ffffff")
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9), background="#e8efed", foreground="#314440")
        style.configure("Horizontal.TProgressbar", troughcolor="#dfe8e5", background="#087e75")
        style.configure("TRadiobutton", background="#ffffff", foreground="#263735", font=("Segoe UI", 9))

    def _build_ui(self) -> None:
        self.main_frame = ttk.Frame(self.root)
        self.main_frame.pack(fill="both", expand=True)
        header = tk.Frame(self.main_frame, bg="#123c3b", padx=22, pady=15)
        header.pack(fill="x")
        title_area = tk.Frame(header, bg="#123c3b")
        title_area.pack(side="left", fill="x", expand=True)
        ttk.Label(title_area, text="MarkitdownTool", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_area, text=self.t("app_subtitle"), style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))
        self.settings_button = ttk.Button(header, text=self.t("settings"), command=self._open_settings)
        self.settings_button.pack(side="right", padx=(8, 0))
        self.configure_languages_button = ttk.Button(header, text=self.t("configure_ocr"), command=self._choose_languages)
        self.configure_languages_button.pack(side="right")

        body = ttk.Frame(self.main_frame, padding=(18, 15, 18, 12))
        body.pack(fill="both", expand=True)

        toolbar = ttk.Frame(body)
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Label(toolbar, text=self.t("documents"), style="Section.TLabel").pack(side="left")
        self.add_button = ttk.Button(toolbar, text="＋  " + self.t("add_files"), command=self._add_files)
        self.add_button.pack(side="right", padx=(8, 0))
        self.add_folder_button = ttk.Button(toolbar, text="＋  " + self.t("add_folder"), command=self._add_folder)
        self.add_folder_button.pack(side="right", padx=(8, 0))
        self.remove_button = ttk.Button(toolbar, text=self.t("remove_selected"), command=self._remove_selected)
        self.remove_button.pack(side="right", padx=(8, 0))
        self.clear_button = ttk.Button(toolbar, text=self.t("clear_all"), command=self._clear_files)
        self.clear_button.pack(side="right")

        table_frame = ttk.Frame(body, style="Panel.TFrame", padding=1)
        table_frame.pack(fill="both", expand=True)
        columns = ("type", "size", "status")
        self.file_table = ttk.Treeview(table_frame, columns=columns, show="tree headings", selectmode="extended")
        self.file_table.heading("#0", text=self.t("file_name"), anchor="w")
        self.file_table.heading("type", text=self.t("format"), anchor="w")
        self.file_table.heading("size", text=self.t("size"), anchor="e")
        self.file_table.heading("status", text=self.t("status"), anchor="w")
        self.file_table.column("#0", width=460, minwidth=220, stretch=True)
        self.file_table.column("type", width=110, minwidth=90, stretch=False)
        self.file_table.column("size", width=100, minwidth=80, stretch=False, anchor="e")
        self.file_table.column("status", width=220, minwidth=160, stretch=True)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.file_table.yview)
        self.file_table.configure(yscrollcommand=scrollbar.set)
        self.file_table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        options = ttk.Frame(body, style="Panel.TFrame", padding=(14, 11))
        options.pack(fill="x", pady=(12, 0))
        ttk.Label(options, text=self.t("processing_mode"), style="Section.TLabel").pack(anchor="w", pady=(0, 7))
        mode_row = ttk.Frame(options, style="Panel.TFrame")
        mode_row.pack(fill="x")
        modes = (
            (ProcessingMode.OCR.value, self.t("ocr_only")),
            (ProcessingMode.MARKDOWN.value, self.t("markdown_only")),
            (ProcessingMode.OCR_MARKDOWN.value, self.t("ocr_and_markdown")),
        )
        for value, label in modes:
            button = ttk.Radiobutton(mode_row, text=label, variable=self.mode, value=value)
            button.pack(side="left", padx=(0, 20))
            self.mode_buttons.append(button)
        ttk.Label(options, textvariable=self.language_label, style="Panel.TLabel").pack(anchor="w", pady=(9, 0))

        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(12, 0))
        self.progress = ttk.Progressbar(footer, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(0, 7))
        status_row = ttk.Frame(footer)
        status_row.pack(fill="x")
        ttk.Label(status_row, textvariable=self.status).pack(side="left", fill="x", expand=True)
        self.open_output_button = ttk.Button(status_row, text=self.t("open_output"), command=self._open_output)
        self.open_output_button.pack(side="right", padx=(8, 0))
        self.cancel_button = ttk.Button(status_row, text=self.t("cancel"), command=self._cancel)
        self.cancel_button.pack(side="right", padx=(8, 0))
        self.start_button = ttk.Button(status_row, text=self.t("starting"), style="Accent.TButton", command=self._start)
        self.start_button.pack(side="right")
        self.cancel_button.state(["disabled"])

    def _open_settings(self) -> None:
        if self.busy:
            return

        window = tk.Toplevel(self.root)
        window.title(self.t("settings_title"))
        window.geometry("760x600")
        window.minsize(650, 500)
        window.transient(self.root)

        notebook = ttk.Notebook(window)
        notebook.pack(fill="both", expand=True, padx=14, pady=14)
        general = ttk.Frame(notebook, padding=18)
        tools = ttk.Frame(notebook, padding=18)
        connection = ttk.Frame(notebook, padding=18)
        about = ttk.Frame(notebook, padding=18)
        notebook.add(general, text=self.t("general"))
        notebook.add(tools, text=self.t("tools"))
        notebook.add(connection, text=self.t("connection"))
        notebook.add(about, text=self.t("about"))

        ttk.Label(general, text=self.t("display_language"), style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 7))
        language_options = [self.t("english"), self.t("vietnamese")]
        language_codes = {language_options[0]: "en", language_options[1]: "vi"}
        language_value = tk.StringVar(value=language_options[0] if self.ui_language == "en" else language_options[1])
        language_combo = ttk.Combobox(general, textvariable=language_value, values=language_options, state="readonly", width=24)
        language_combo.grid(row=1, column=0, sticky="w")
        ttk.Button(general, text=self.t("apply_language"), command=lambda: apply_language()).grid(row=1, column=1, padx=(10, 0), sticky="w")

        ttk.Separator(general).grid(row=2, column=0, columnspan=3, sticky="ew", pady=18)
        ttk.Label(general, text=self.t("data_directory"), style="Section.TLabel").grid(row=3, column=0, sticky="w", pady=(0, 7))
        data_path = tk.StringVar(value=str(DATA_DIR))
        ttk.Entry(general, textvariable=data_path).grid(row=4, column=0, sticky="ew")
        ttk.Button(general, text=self.t("browse"), command=lambda: browse_data_directory()).grid(row=4, column=1, padx=(8, 0))
        data_status = tk.StringVar(value="")
        data_button = ttk.Button(general, text=self.t("change_data_directory"), command=lambda: change_data_directory())
        data_button.grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Label(general, textvariable=data_status, wraplength=560).grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))

        ttk.Separator(general).grid(row=7, column=0, columnspan=3, sticky="ew", pady=18)
        ttk.Label(general, text=self.t("check_updates"), style="Section.TLabel").grid(row=8, column=0, sticky="w", pady=(0, 7))
        update_status = tk.StringVar(value=self.t("update_security_note"))
        update_button = ttk.Button(general, text=self.t("check_updates"), command=lambda: check_updates())
        update_button.grid(row=9, column=0, sticky="w")
        ttk.Label(general, textvariable=update_status, wraplength=620).grid(row=10, column=0, columnspan=3, sticky="w", pady=(8, 0))
        general.columnconfigure(0, weight=1)

        ttk.Label(tools, text=self.t("tool_versions"), style="Section.TLabel").pack(anchor="w", pady=(0, 10))
        components = (
            ("MarkitdownTool", APP_VERSION),
            ("Microsoft MarkItDown", self._package_version("markitdown")),
            ("pdfplumber", self._package_version("pdfplumber")),
            ("PDFium (pypdfium2)", self._package_version("pypdfium2")),
            ("OCRmyPDF", self._package_version("ocrmypdf")),
            ("Magika", self._package_version("magika")),
            ("Tesseract OCR", self._tesseract_version()),
        )
        for name, version in components:
            row = ttk.Frame(tools)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=name, width=28).pack(side="left", anchor="w")
            ttk.Label(row, text=version).pack(side="left", anchor="w")
        ttk.Separator(tools).pack(fill="x", pady=14)
        tool_actions = ttk.Frame(tools)
        tool_actions.pack(fill="x")
        ttk.Button(tool_actions, text=self.t("manage_languages"), command=lambda: self._open_language_manager(window)).pack(side="left")
        ttk.Button(tool_actions, text=self.t("select_languages"), command=lambda: self._choose_languages()).pack(side="left", padx=(8, 0))

        ttk.Label(connection, text=self.t("connection_intro"), wraplength=680).pack(anchor="w", pady=(0, 10))
        cli = subprocess.list2cmdline(cli_command())
        mcp_command, *mcp_args = cli_command("mcp")
        mcp_config = json.dumps({"mcpServers": {"markitdowntool": {"command": mcp_command, "args": mcp_args}}}, indent=2)
        for title_key, hint_key, snippet in (
            ("cli_title", "cli_hint", f"{cli} convert \"D:\\Docs\" --recursive --mode markdown --json"),
            ("api_title", "api_hint", f"{cli} api --port 8765"),
            ("mcp_title", "mcp_hint", mcp_config),
        ):
            self._connection_snippet(connection, self.t(title_key), self.t(hint_key), snippet)

        ttk.Label(about, text=self.t("about_version", version=APP_VERSION), style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(about, text=self.t("about_author")).pack(anchor="w", pady=(0, 8))
        ttk.Button(about, text=self.t("open_repository"), command=lambda: webbrowser.open(f"https://github.com/{GITHUB_REPOSITORY}")).pack(anchor="w", pady=(0, 12))
        for title_key, filename in (
            ("open_license", "LICENSE.md"),
            ("open_disclaimer", "DISCLAIMER.md"),
            ("open_privacy", "PRIVACY.md"),
            ("open_notices", "THIRD_PARTY_NOTICES.md"),
        ):
            ttk.Button(about, text=self.t(title_key), command=lambda name=filename: self._open_legal_file(name, window)).pack(anchor="w", pady=3)

        def apply_language() -> None:
            selected_language = language_codes.get(language_value.get(), "en")
            if selected_language == self.ui_language:
                window.destroy()
                return
            self.config["ui_language"] = selected_language
            save_config(self.config)
            self.ui_language = selected_language
            window.destroy()
            self.main_frame.destroy()
            self.file_rows.clear()
            self._build_ui()
            for path in self.files:
                self._insert_file(path)
            self._refresh_language_label()

        def browse_data_directory() -> None:
            selected = filedialog.askdirectory(parent=window, initialdir=data_path.get() or str(DATA_DIR))
            if selected:
                data_path.set(selected)

        def change_data_directory() -> None:
            selected = Path(data_path.get()).expanduser()
            if not selected.is_absolute():
                messagebox.showerror(self.t("settings_title"), self.t("invalid_data_directory"), parent=window)
                return
            if selected.resolve() == DATA_DIR.resolve():
                data_status.set(self.t("data_directory_unchanged"))
                return
            if not messagebox.askyesno(self.t("data_directory"), self.t("copy_data_prompt"), parent=window):
                return
            data_button.state(["disabled"])
            data_status.set(self.t("copying_data"))

            def copy_data() -> None:
                try:
                    copy_data_preserving_existing(DATA_DIR, selected)
                    self.events.put(("data_directory_changed", (selected, window, data_status, data_button)))
                except Exception as error:
                    self.events.put(("data_directory_error", (str(error), window, data_status, data_button)))

            threading.Thread(target=copy_data, daemon=True).start()

        def check_updates() -> None:
            update_button.state(["disabled"])
            update_status.set(self.t("checking_updates"))

            def request_release() -> None:
                request = urllib.request.Request(
                    GITHUB_RELEASES_API,
                    headers={"Accept": "application/vnd.github+json", "User-Agent": "MarkitdownTool"},
                )
                try:
                    with urllib.request.urlopen(request, timeout=15) as response:
                        release = json.loads(response.read().decode("utf-8"))
                    self.root.after(0, lambda: show_release(release))
                except Exception as error:
                    self.root.after(0, lambda error=str(error): finish_update_error(error))

            def show_release(release: dict) -> None:
                update_button.state(["!disabled"])
                latest = str(release.get("tag_name", "")).lstrip("vV")
                published = str(release.get("published_at", ""))[:10] or "unknown"
                release_url = release.get("html_url") or f"https://github.com/{GITHUB_REPOSITORY}/releases"
                if not latest:
                    update_status.set(self.t("no_releases"))
                    return
                if self._version_key(latest) > self._version_key(APP_VERSION):
                    update_status.set(self.t("release_details", version=latest, published=published, notes=self.t("update_security_note")))
                    if messagebox.askyesno(self.t("check_updates"), self.t("update_available", latest=latest), parent=window):
                        webbrowser.open(release_url)
                else:
                    update_status.set(self.t("update_current", version=APP_VERSION))

            def finish_update_error(error: str) -> None:
                update_button.state(["!disabled"])
                update_status.set(self.t("update_check_failed", error=error))

            threading.Thread(target=request_release, daemon=True).start()

        window.grab_set()

    def _connection_snippet(self, parent: tk.Misc, title: str, hint: str, snippet: str) -> None:
        ttk.Label(parent, text=title, style="Section.TLabel").pack(anchor="w", pady=(8, 2))
        ttk.Label(parent, text=hint, wraplength=680).pack(anchor="w")
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(4, 0))
        lines = snippet.count("\n") + 1
        text = tk.Text(row, height=min(lines, 8), wrap="none", font=("Consolas", 9), relief="solid", borderwidth=1)
        text.insert("1.0", snippet)
        text.configure(state="disabled")
        text.pack(side="left", fill="x", expand=True)
        button = ttk.Button(row, text=self.t("copy"))

        def copy() -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(snippet)
            button.configure(text=self.t("copied"))

        button.configure(command=copy)
        button.pack(side="left", anchor="n", padx=(8, 0))

    @staticmethod
    def _version_key(value: str) -> tuple[int, ...]:
        import re

        return tuple(int(part) for part in re.findall(r"\d+", value)) or (0,)

    @staticmethod
    def _package_version(package: str) -> str:
        try:
            return importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            return "Not installed"

    @staticmethod
    def _tesseract_version() -> str:
        executable = find_tesseract(APP_DIR)
        if not executable:
            return "Not installed"
        try:
            result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
            return result.stdout.splitlines()[0] if result.stdout else "Unknown"
        except Exception:
            return "Unknown"

    def _open_legal_file(self, filename: str, parent: tk.Misc) -> None:
        path = find_application_resource(
            filename,
            APP_DIR,
            Path(getattr(sys, "_MEIPASS", APP_DIR)),
            Path(__file__).parent,
        )
        if path is None:
            messagebox.showerror(self.t("about"), self.t("legal_file_missing", filename=filename), parent=parent)
            return
        try:
            os.startfile(str(path))
        except Exception as error:
            messagebox.showerror(self.t("cannot_open_folder"), str(error), parent=parent)

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self.root,
            title=self.t("choose_document"),
            filetypes=[
                (self.t("filetype_documents_images"), "*.pdf *.docx *.pptx *.xlsx *.png *.jpg *.jpeg *.tif *.tiff *.bmp"),
                (self.t("filetype_all"), "*.*"),
            ],
        )
        self._append_files(Path(selected) for selected in paths)
        self.status.set(self.t("files_selected", count=len(self.files)))

    def _add_folder(self) -> None:
        selected = filedialog.askdirectory(parent=self.root, title=self.t("choose_folder"), mustexist=True)
        if not selected:
            return
        folder = Path(selected)
        try:
            recursive = has_subfolders(folder) and messagebox.askyesno(
                self.t("add_folder"),
                self.t("include_subfolders_prompt", folder=folder.name or str(folder)),
                parent=self.root,
            )
            found = collect_supported_files(folder, recursive=recursive)
        except OSError as error:
            messagebox.showerror(self.t("add_folder"), str(error), parent=self.root)
            return
        if not found:
            self.status.set(self.t("folder_no_supported_files", folder=folder.name or str(folder)))
            return
        added = self._append_files(found)
        self.status.set(self.t("folder_files_added", added=added, found=len(found), count=len(self.files)))

    def _append_files(self, paths) -> int:
        existing = {str(path.resolve()).casefold() for path in self.files}
        added = 0
        for path in paths:
            key = str(path.resolve()).casefold()
            if key not in existing:
                self.files.append(path)
                existing.add(key)
                self._insert_file(path)
                added += 1
        return added

    def _insert_file(self, path: Path) -> None:
        try:
            size_text = self._format_size(path.stat().st_size)
        except OSError:
            size_text = self.t("file_size_unknown")
        extension = path.suffix.lower()
        supported = extension in SUPPORTED_EXTENSIONS
        initial_status = self.t("file_status_check_pdf") if extension == ".pdf" else (self.t("file_status_ready") if supported else self.t("file_status_unsupported"))
        item_id = self.file_table.insert("", "end", text=path.name, values=(extension.upper().lstrip("."), size_text, initial_status))
        self.file_rows[item_id] = path

    @staticmethod
    def _format_size(size: int) -> str:
        units = ("B", "KB", "MB", "GB")
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024
        return f"{size} B"

    def _remove_selected(self) -> None:
        selected = set(self.file_table.selection())
        if not selected:
            return
        for item_id in selected:
            self.file_table.delete(item_id)
            self.file_rows.pop(item_id, None)
        self.files = list(self.file_rows.values())
        self.status.set(self.t("files_remaining", count=len(self.files)))

    def _clear_files(self) -> None:
        if self.busy:
            return
        self.files.clear()
        self.file_rows.clear()
        self.file_table.delete(*self.file_table.get_children())
        self.progress.configure(value=0)
        self.status.set(self.t("files_cleared"))

    def _refresh_language_label(self) -> None:
        selected = ", ".join(self.languages) if self.languages else self.t("languages_not_selected")
        self.language_label.set(self.t("output_languages", languages=selected))

    def _choose_languages(self) -> None:
        if self.busy:
            return
        try:
            packages = list_language_packages(APP_DIR, DATA_DIR)
            available = [package.code for package in packages]
        except Exception as error:
            messagebox.showerror(self.t("language_error"), self.t("cannot_read_languages", error=error), parent=self.root)
            return

        dialog = tk.Toplevel(self.root)
        dialog.title(self.t("ocr_configuration"))
        dialog.transient(self.root)
        dialog.resizable(True, True)
        dialog.geometry("430x470")
        dialog.minsize(360, 360)
        ttk.Label(dialog, text=self.t("choose_ocr_languages"), padding=(14, 14, 14, 4)).pack(anchor="w")
        listbox = tk.Listbox(dialog, selectmode=tk.MULTIPLE, exportselection=False, height=14, font=("Segoe UI", 10))
        listbox.pack(fill="both", expand=True, padx=14, pady=8)
        for index, code in enumerate(available):
            listbox.insert(tk.END, code)
            if code in self.languages:
                listbox.selection_set(index)

        buttons = ttk.Frame(dialog, padding=(14, 0, 14, 14))
        buttons.pack(fill="x")
        ttk.Button(buttons, text=self.t("manage_packages"), command=lambda: self._open_language_manager(dialog, refresh=lambda: self._refresh_language_list(listbox))).pack(side="left")

        def accept() -> None:
            try:
                current_available = [package.code for package in list_language_packages(APP_DIR, DATA_DIR)]
            except Exception as error:
                messagebox.showerror(self.t("ocr_configuration"), self.t("cannot_read_languages", error=error), parent=dialog)
                return
            selected = [current_available[index] for index in listbox.curselection() if index < len(current_available)]
            if not selected:
                messagebox.showinfo(self.t("ocr_configuration"), self.t("select_one_language"), parent=dialog)
                return
            self.languages = selected
            config = load_config()
            config["ocr_languages"] = selected
            save_config(config)
            self._refresh_language_label()
            dialog.destroy()

        ttk.Button(buttons, text=self.t("close"), command=dialog.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text=self.t("save_selection"), command=accept).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()

    def _refresh_language_list(self, listbox: tk.Listbox) -> None:
        try:
            packages = list_language_packages(APP_DIR, DATA_DIR)
        except Exception:
            return
        previous = set(self.languages)
        listbox.delete(0, tk.END)
        for index, package in enumerate(packages):
            listbox.insert(tk.END, package.code)
            if package.code in previous:
                listbox.selection_set(index)

    def _open_language_manager(self, parent: tk.Misc | None = None, refresh=None) -> None:
        if self.busy:
            return
        owner = parent or self.root
        window = tk.Toplevel(owner)
        window.title(self.t("language_manager_title"))
        window.geometry("690x570")
        window.minsize(560, 450)
        window.transient(owner)

        container = ttk.Frame(window, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(2, weight=1)
        ttk.Label(container, text=self.t("language_packages"), style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        search_var = tk.StringVar()
        search_row = ttk.Frame(container)
        search_row.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(search_row, text=self.t("search_languages")).pack(side="left", padx=(0, 8))
        search_entry = ttk.Entry(search_row, textvariable=search_var)
        search_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(
            search_row,
            text=self.t("language_code_catalog"),
            command=lambda: webbrowser.open("https://tesseract-ocr.github.io/tessdoc/Data-Files.html"),
        ).pack(side="left", padx=(8, 0))

        columns = ("source", "removable")
        table = ttk.Treeview(container, columns=columns, show="tree headings", height=11, selectmode="browse")
        table.heading("#0", text=self.t("language_code"), anchor="w")
        table.heading("source", text=self.t("source"), anchor="w")
        table.heading("removable", text=self.t("removable"), anchor="center")
        table.column("#0", width=120, stretch=False)
        table.column("source", width=210, stretch=True)
        table.column("removable", width=100, stretch=False, anchor="center")
        table.grid(row=2, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=2, column=1, sticky="ns")
        status = tk.StringVar(value="")
        ttk.Label(container, textvariable=status, wraplength=620).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 6))

        install_row = ttk.Frame(container)
        install_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Label(install_row, text=self.t("language_code")).pack(side="left")
        code_entry = ttk.Entry(install_row, width=15)
        code_entry.pack(side="left", padx=(8, 8))
        code_entry.insert(0, "vie")
        install_button = ttk.Button(install_row, text=self.t("download_install"), command=lambda: install_selected())
        install_button.pack(side="left")

        action_row = ttk.Frame(container)
        action_row.grid(row=5, column=0, columnspan=2, sticky="ew")
        ttk.Button(action_row, text=self.t("remove_package"), command=lambda: remove_selected()).pack(side="left")
        ttk.Button(action_row, text=self.t("refresh"), command=lambda: refresh_table()).pack(side="left", padx=(8, 0))
        ttk.Button(action_row, text=self.t("close"), command=window.destroy).pack(side="right")
        manager_events: queue.Queue = queue.Queue()
        installing = False
        current_packages = []

        def refresh_table() -> None:
            nonlocal current_packages
            table.delete(*table.get_children())
            try:
                current_packages = list_language_packages(APP_DIR, DATA_DIR)
            except Exception as error:
                status.set(self.t("cannot_read_languages", error=error))
                return
            query = search_var.get().strip().casefold()
            for package in current_packages:
                if query and query not in package.code.casefold():
                    continue
                removable = package.removable and package.code not in self.languages
                source = self.t("source_app") if package.source == "Ứng dụng" else self.t("source_system")
                table.insert("", "end", iid=package.code, text=package.code, values=(source, self.t("yes") if removable else self.t("no")))
            if refresh:
                refresh()

        def install_selected() -> None:
            nonlocal installing
            code = code_entry.get().strip()
            if not code:
                status.set(self.t("enter_language_code"))
                return
            try:
                installed_codes = {item.code for item in current_packages}
            except Exception as error:
                status.set(self.t("cannot_read_languages", error=error))
                return
            if code in installed_codes:
                status.set(self.t("already_available", code=code))
                return
            installing = True
            install_button.state(["disabled"])
            status.set(self.t("downloading_language", code=code))

            def download() -> None:
                try:
                    installed = install_language(code, APP_DIR, DATA_DIR)
                    manager_events.put(("installed", str(installed)))
                except Exception as error:
                    manager_events.put(("error", str(error)))

            threading.Thread(target=download, daemon=True).start()
            window.after(100, poll_manager_events)

        def remove_selected() -> None:
            selected = table.selection()
            if not selected:
                status.set(self.t("select_package"))
                return
            code = selected[0]
            try:
                package = next((item for item in current_packages if item.code == code), None)
            except Exception as error:
                status.set(self.t("cannot_read_languages", error=error))
                return
            if code == "osd":
                status.set(self.t("cannot_remove_osd"))
                return
            if not package or not package.removable:
                status.set(self.t("system_package_readonly"))
                return
            if code in self.languages:
                status.set(self.t("language_in_use", code=code))
                return
            if not messagebox.askyesno(self.t("remove_package"), self.t("confirm_uninstall_language", code=code), parent=window):
                return
            try:
                uninstall_language(code, APP_DIR, DATA_DIR)
                if code in self.languages:
                    self.languages.remove(code)
                    config = load_config()
                    config["ocr_languages"] = self.languages
                    save_config(config)
                    self._refresh_language_label()
                status.set(self.t("language_uninstalled", code=code))
                refresh_table()
            except Exception as error:
                messagebox.showerror(self.t("uninstall_failed"), str(error), parent=window)

        def poll_manager_events() -> None:
            nonlocal installing
            try:
                kind, value = manager_events.get_nowait()
            except queue.Empty:
                if installing and window.winfo_exists():
                    window.after(100, poll_manager_events)
                return
            installing = False
            install_button.state(["!disabled"])
            if kind == "installed":
                status.set(self.t("installed_language", filename=Path(value).name))
                refresh_table()
            else:
                status.set(self.t("install_failed", error=value))

        refresh_table()
        search_var.trace_add("write", lambda *_args: refresh_table())
        window.grab_set()

    def _start(self) -> None:
        if self.busy:
            return
        if not self.files:
            messagebox.showinfo(self.t("no_documents"), self.t("add_at_least_one"), parent=self.root)
            return

        mode = ProcessingMode(self.mode.get())
        logging.info("File check started: mode=%s files=%d", mode.value, len(self.files))
        if mode is not ProcessingMode.MARKDOWN and not self.languages:
            messagebox.showinfo(self.t("language_error"), self.t("choose_ocr_before_start"), parent=self.root)
            self._choose_languages()
            return

        self.cancel_event = threading.Event()
        self._set_busy(True, self.t("checking_files"), cancellable=True)
        self.progress.configure(value=0, maximum=max(len(self.files), 1))

        def preflight() -> None:
            try:
                classifications = []
                for path in self.files:
                    if self.cancel_event.is_set():
                        self.events.put(("preflight_cancelled", None))
                        return
                    classifications.append(classify_file(path))
                ready = sum(item.supported for item in classifications)
                needs_ocr = sum(item.needs_ocr for item in classifications)
                logging.info(
                    "File check completed: files=%d supported=%d needs_ocr=%d",
                    len(classifications),
                    ready,
                    needs_ocr,
                )
                self.events.put(("preflight", (mode, classifications)))
            except Exception as error:
                logging.exception("File check failed")
                self.events.put(("preflight_error", str(error)))

        threading.Thread(target=preflight, daemon=True).start()

    def _handle_preflight(self, mode: ProcessingMode, classifications: tuple[FileClassification, ...]) -> None:
        self.classifications = classifications
        for item_id, path in self.file_rows.items():
            classification = next((entry for entry in classifications if entry.path == path), None)
            if classification:
                status = self.t("file_status_ocr_first") if classification.needs_ocr else (self.t("file_status_ready") if classification.supported else self.t("file_status_unsupported"))
                self.file_table.set(item_id, "status", status)

        invalid = [item for item in classifications if not item.markdown_eligible]
        if mode is ProcessingMode.MARKDOWN and invalid:
            count = len(invalid)
            names = "\n".join(f"- {item.path.name}: {self._translate_reason(item.reason)}" for item in invalid[:8])
            if count > 8:
                names += f"\n... và {count - 8} tệp khác"
            proceed = messagebox.askyesno(
                self.t("markdown_invalid_title"),
                self.t("markdown_invalid_prompt", count=count, details=names),
                parent=self.root,
            )
            if not proceed:
                self._set_busy(False, self.t("session_declined"))
                return

        if mode is ProcessingMode.OCR:
            eligible = [item for item in classifications if item.ocr_eligible]
        elif mode is ProcessingMode.MARKDOWN:
            eligible = [item for item in classifications if item.markdown_eligible]
        else:
            eligible = [item for item in classifications if item.supported]
        if not eligible:
            self._set_busy(False, self.t("no_eligible_files"))
            return

        needs_ocr = mode is ProcessingMode.OCR or (
            mode is ProcessingMode.OCR_MARKDOWN
            and any(item.needs_ocr for item in eligible)
        )
        if needs_ocr:
            if not find_tesseract(APP_DIR):
                self._set_busy(False, self.t("tesseract_missing_status"))
                messagebox.showerror(
                    self.t("tesseract_missing_title"),
                    self.t("tesseract_missing_message"),
                    parent=self.root,
                )
                return
            try:
                available_languages = set(get_available_languages(APP_DIR, DATA_DIR))
            except Exception as error:
                self._set_busy(False, self.t("language_check_failed"))
                messagebox.showerror(self.t("language_error"), self.t("cannot_read_languages", error=error), parent=self.root)
                return
            missing_languages = [code for code in self.languages if code not in available_languages]
            if missing_languages:
                self._set_busy(False, self.t("missing_languages_status"))
                messagebox.showerror(
                    self.t("missing_languages_title"),
                    self.t("missing_languages_message", languages=", ".join(missing_languages)),
                    parent=self.root,
                )
                return

        self.cancel_event = threading.Event()
        self._set_busy(True, self.t("processing_preparing"), cancellable=True)

        def progress_callback(update: ProgressUpdate) -> None:
            self.events.put(("progress", update))

        def process() -> None:
            try:
                result = process_batch(
                    [item.path for item in classifications],
                    mode,
                    list(self.languages),
                    APP_DIR,
                    OCRPDF_DIR,
                    MARKDOWN_DIR,
                    cancel_event=self.cancel_event,
                    progress_callback=progress_callback,
                    classifications=classifications,
                    data_dir=DATA_DIR,
                )
                self.events.put(("finished", result))
            except Exception as error:
                logging.exception("Batch processing failed")
                self.events.put(("processing_error", str(error)))

        threading.Thread(target=process, daemon=True).start()

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "preflight":
                    mode, classifications = payload
                    self._handle_preflight(mode, classifications)
                elif kind == "preflight_error":
                    self._set_busy(False, self.t("preflight_failed"))
                    messagebox.showerror(self.t("preflight_title"), payload, parent=self.root)
                elif kind == "preflight_cancelled":
                    if self.close_when_finished:
                        self.root.destroy()
                        return
                    self._set_busy(False, self.t("preflight_cancelled"))
                elif kind == "progress":
                    update: ProgressUpdate = payload
                    self.progress.configure(value=update.completed)
                    self.status.set(self.t("file_progress", completed=update.completed, total=update.total, filename=update.path.name, stage=self._translate_stage(update.stage)))
                elif kind == "finished":
                    self._show_results(payload)
                elif kind == "processing_error":
                    self._set_busy(False, self.t("processing_failed"))
                    messagebox.showerror(self.t("processing_failed"), payload, parent=self.root)
                elif kind == "data_directory_changed":
                    selected, window, status, button = payload
                    self.config["data_directory"] = str(selected)
                    save_config(self.config)
                    save_data_directory_registry(selected)
                    if window.winfo_exists():
                        status.set(self.t("data_dir_saved"))
                        button.state(["!disabled"])
                elif kind == "data_directory_error":
                    error, window, status, button = payload
                    if window.winfo_exists():
                        status.set(str(error))
                        button.state(["!disabled"])
                elif kind == "legacy_output_migration_done":
                    self.migrating_legacy_output = False
                    self.config["legacy_output_migration_checked"] = True
                    save_config(self.config)
                    self._set_busy(False, self.t("legacy_output_copied"))
                    if self.close_when_finished:
                        self.root.destroy()
                        return
                elif kind == "legacy_output_migration_error":
                    self.migrating_legacy_output = False
                    self._set_busy(False, self.t("legacy_output_copy_failed", error=payload))
                    if self.close_when_finished:
                        self.root.destroy()
                        return
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._poll_events)

    def _show_results(self, result: BatchResult) -> None:
        self._set_busy(False, self.t("session_cancelled") if result.cancelled else self.t("session_complete"))
        successful = sum(item.status is FileStatus.SUCCESS for item in result.files)
        skipped = sum(item.status is FileStatus.SKIPPED for item in result.files)
        failed = sum(item.status is FileStatus.FAILED for item in result.files)
        lines = [self.t("result_counts", success=successful, skipped=skipped, failed=failed)]
        for item in result.files:
            detail = ", ".join(path.name for path in item.outputs) or item.message
            lines.append(f"{item.path.name} · {self._translate_stage(item.stage)}: {detail}")
            if item.status is FileStatus.FAILED:
                logging.error("%s: %s", item.path, item.message)

        dialog = tk.Toplevel(self.root)
        dialog.title(self.t("result_title"))
        dialog.geometry("650x390")
        dialog.transient(self.root)
        ttk.Label(dialog, text=lines[0], padding=(14, 14, 14, 8), style="Section.TLabel").pack(anchor="w")
        text_frame = ttk.Frame(dialog, padding=(14, 0, 14, 10))
        text_frame.pack(fill="both", expand=True)
        details = tk.Text(text_frame, wrap="word", font=("Segoe UI", 9), relief="solid", borderwidth=1)
        scrollbar = ttk.Scrollbar(text_frame, orient="vertical", command=details.yview)
        details.configure(yscrollcommand=scrollbar.set)
        details.insert("1.0", "\n".join(lines[1:]))
        details.configure(state="disabled")
        details.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        buttons = ttk.Frame(dialog, padding=(14, 0, 14, 14))
        buttons.pack(fill="x")
        ttk.Button(buttons, text=self.t("open_markdown"), command=lambda: self._open_location(MARKDOWN_DIR)).pack(side="left")
        ttk.Button(buttons, text=self.t("open_ocr_pdf"), command=lambda: self._open_location(OCRPDF_DIR)).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text=self.t("close"), command=dialog.destroy).pack(side="right")
        dialog.grab_set()
        if self.close_when_finished:
            dialog.destroy()
            self.root.destroy()

    def _set_busy(self, busy: bool, status: str, cancellable: bool = False) -> None:
        self.busy = busy
        self.status.set(status)
        self.start_button.state(["disabled"] if busy else ["!disabled"])
        self.add_button.state(["disabled"] if busy else ["!disabled"])
        self.add_folder_button.state(["disabled"] if busy else ["!disabled"])
        self.remove_button.state(["disabled"] if busy else ["!disabled"])
        self.clear_button.state(["disabled"] if busy else ["!disabled"])
        self.settings_button.state(["disabled"] if busy else ["!disabled"])
        self.configure_languages_button.state(["disabled"] if busy else ["!disabled"])
        for button in self.mode_buttons:
            button.state(["disabled"] if busy else ["!disabled"])
        self.cancel_button.state(["!disabled"] if busy and cancellable else ["disabled"])
        if not busy:
            self.cancel_button.configure(text=self.t("cancel"))

    def _cancel(self) -> None:
        if self.cancel_event and not self.cancel_event.is_set():
            self.cancel_event.set()
            self.cancel_button.state(["disabled"])
            self.cancel_button.configure(text=self.t("cancel"))
            self.status.set(self.t("cancel_pending"))

    def _open_output(self) -> None:
        self._open_location(OUTPUT_DIR)

    def _offer_legacy_output_migration(self) -> None:
        if not IS_FROZEN or self.config.get("legacy_output_migration_checked"):
            return
        legacy_output = APP_DIR / "output"
        if not legacy_output.is_dir() or legacy_output.resolve() == OUTPUT_DIR.resolve():
            self.config["legacy_output_migration_checked"] = True
            save_config(self.config)
            return
        if not messagebox.askyesno(
            self.t("data_directory"),
            self.t("legacy_output_prompt"),
            parent=self.root,
        ):
            self.config["legacy_output_migration_checked"] = True
            save_config(self.config)
            self.status.set(self.t("legacy_output_kept"))
            return

        self._set_busy(True, self.t("legacy_output_copying"))
        self.migrating_legacy_output = True
        self.cancel_button.state(["disabled"])

        def copy_legacy_output() -> None:
            try:
                copy_data_preserving_existing(legacy_output, OUTPUT_DIR)
                self.events.put(("legacy_output_migration_done", None))
            except Exception as error:
                self.events.put(("legacy_output_migration_error", str(error)))

        threading.Thread(target=copy_legacy_output, daemon=True).start()

    def _open_location(self, path: Path) -> None:
        try:
            os.startfile(str(path))
        except Exception as error:
            messagebox.showerror(self.t("cannot_open_folder"), str(error), parent=self.root)

    def _on_close(self) -> None:
        if self.busy:
            if self.migrating_legacy_output:
                if messagebox.askyesno(
                    self.t("processing_active"),
                    self.t("legacy_output_close"),
                    parent=self.root,
                ):
                    self.close_when_finished = True
                return
            should_stop = messagebox.askyesno(
                self.t("processing_active"),
                self.t("close_after_current"),
                parent=self.root,
            )
            if should_stop:
                self.close_when_finished = True
                self._cancel()
            return
        self.root.destroy()


def main() -> None:
    logging.info("Application started: version=%s", APP_VERSION)
    root = tk.Tk()
    app = MarkitdownApp(root)
    root.after(300, app._offer_legacy_output_migration)
    root.mainloop()


if __name__ == "__main__":
    if not getattr(sys, "frozen", False) and os.name == "nt" and not is_running_in_venv():
        relaunch_in_venv()
    main()