# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
from pathlib import Path

SPEC_DIR = Path(SPECPATH)

datas = []
binaries = []
# office_converter imports pywin32 inside functions, so PyInstaller cannot see it.
hiddenimports = ['pythoncom', 'pywintypes', 'win32com', 'win32com.client', 'win32gui', 'win32process']
tmp_ret = collect_all('markitdown')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('magika')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('ocrmypdf')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pypdfium2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
datas += [
    (str(SPEC_DIR / filename), ".")
    for filename in ("LICENSE.md", "DISCLAIMER.md", "PRIVACY.md", "THIRD_PARTY_NOTICES.md", "README.md")
]


a = Analysis(
    ['markitdown_gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['fitz', 'pymupdf'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='markitdown_gui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)


# Console executable for the CLI, HTTP API, and MCP (stdio) integrations.
cli_datas = list(datas)
cli_binaries = list(binaries)
cli_hiddenimports = list(hiddenimports)
for package in ('mcp', 'mcp_types'):
    # mcp.cli is the SDK's own developer CLI and needs typer, which is not installed.
    tmp_ret = collect_all(package, filter_submodules=lambda name: not name.startswith('mcp.cli'))
    cli_datas += tmp_ret[0]; cli_binaries += tmp_ret[1]; cli_hiddenimports += tmp_ret[2]

cli_a = Analysis(
    ['markitdown_cli.py'],
    pathex=[],
    binaries=cli_binaries,
    datas=cli_datas,
    hiddenimports=cli_hiddenimports + ['api_server', 'mcp_server', 'integration_service'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['fitz', 'pymupdf', 'tkinter'],
    noarchive=False,
    optimize=0,
)
cli_pyz = PYZ(cli_a.pure)

cli_exe = EXE(
    cli_pyz,
    cli_a.scripts,
    cli_a.binaries,
    cli_a.datas,
    [],
    name='markitdowntool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
