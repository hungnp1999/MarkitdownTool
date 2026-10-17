# Bumps the release version in app_runtime.py, the NSIS script and the README title,
# and turns CHANGELOG's [Unreleased] heading into a dated [X.Y.Z] section.
# Checks every edit before writing any file, and keeps each file's encoding (BOM or not) and line endings.
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Date = (Get-Date -Format "yyyy-MM-dd")
)
$ErrorActionPreference = "Stop"

if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Version must look like X.Y.Z (got '$Version')." }
$root = (git rev-parse --show-toplevel).Trim()

# (?=\r?$) instead of $ because the files use CRLF and .NET's multiline $ only matches before \n.
$edits = @(
    @("app_runtime.py", '^APP_VERSION = "[^"]+"(?=\r?$)', "APP_VERSION = `"$Version`""),
    @("installer\MarkitdownTool.nsi", '^!define PRODUCT_VERSION "[^"]+"(?=\r?$)', "!define PRODUCT_VERSION `"$Version`""),
    @("installer\MarkitdownTool.nsi", '^VIProductVersion "[^"]+"(?=\r?$)', "VIProductVersion `"$Version.0`""),
    @("README.md", '^(\W*)# MarkitdownTool \S+(?=\r?$)', "`${1}# MarkitdownTool $Version"),
    @("CHANGELOG.md", '^## \[Unreleased\][ \t]*(?=\r?$)', "## [$Version] - $Date")
)

$files = @{}
foreach ($relativePath in ($edits | ForEach-Object { $_[0] } | Select-Object -Unique)) {
    $path = Join-Path $root $relativePath
    $bytes = [IO.File]::ReadAllBytes($path)
    $files[$relativePath] = @{
        Path   = $path
        HasBom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
        Text   = [IO.File]::ReadAllText($path)
    }
}

$changelog = $files["CHANGELOG.md"].Text
if ([regex]::IsMatch($changelog, "^## \[$([regex]::Escape($Version))\]", "Multiline")) {
    throw "CHANGELOG.md already has a [$Version] section."
}
$unreleased = [regex]::Match($changelog, '(?ms)^## \[Unreleased\][ \t]*\r?\n(.*?)(?=^## \[|\z)')
if (-not $unreleased.Success) { throw "CHANGELOG.md has no '## [Unreleased]' section." }
if (-not $unreleased.Groups[1].Value.Trim()) { throw "The [Unreleased] section in CHANGELOG.md is empty; nothing to release." }

foreach ($edit in $edits) {
    $file = $files[$edit[0]]
    $count = [regex]::Matches($file.Text, $edit[1], "Multiline").Count
    if ($count -ne 1) { throw "$($edit[0]): expected exactly one match for $($edit[1]), found $count. No files were changed." }
    $file.Text = [regex]::Replace($file.Text, $edit[1], $edit[2], "Multiline")
}

foreach ($relativePath in $files.Keys) {
    $file = $files[$relativePath]
    [IO.File]::WriteAllText($file.Path, $file.Text, (New-Object Text.UTF8Encoding($file.HasBom)))
    "updated $relativePath"
}
