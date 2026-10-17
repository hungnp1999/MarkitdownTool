# Reports the version recorded in every place a release touches.
# With -Expected, exits 1 unless all of them equal that version and CHANGELOG has a dated section for it.
param(
    [string]$Expected
)
$ErrorActionPreference = "Stop"

$root = (git rev-parse --show-toplevel).Trim()

function Find-Value([string]$relativePath, [string]$pattern) {
    $text = [IO.File]::ReadAllText((Join-Path $root $relativePath))
    $match = [regex]::Match($text, $pattern, "Multiline")
    if ($match.Success) { return $match.Groups[1].Value }
    return "<not found>"
}

$viProduct = Find-Value "installer\MarkitdownTool.nsi" '^VIProductVersion "([^"]+)"'
$values = [ordered]@{
    "app_runtime.py APP_VERSION"          = Find-Value "app_runtime.py" '^APP_VERSION = "([^"]+)"'
    "MarkitdownTool.nsi PRODUCT_VERSION"  = Find-Value "installer\MarkitdownTool.nsi" '^!define PRODUCT_VERSION "([^"]+)"'
    "MarkitdownTool.nsi VIProductVersion" = $viProduct -replace '\.0$', ''
    "README.md title"                     = Find-Value "README.md" '^\W*# MarkitdownTool (\S+)'
    "CHANGELOG.md latest release"         = Find-Value "CHANGELOG.md" '^## \[(\d+\.\d+\.\d+)\]'
}

$failed = $false
foreach ($key in $values.Keys) {
    $status = ""
    if ($Expected) {
        if ($values[$key] -eq $Expected) { $status = "ok" } else { $status = "MISMATCH"; $failed = $true }
    }
    "{0,-38} {1,-12} {2}" -f $key, $values[$key], $status
}

if ($Expected) {
    if ($viProduct -ne "$Expected.0") {
        "VIProductVersion must be $Expected.0 (found $viProduct)"
        $failed = $true
    }
    $changelog = [IO.File]::ReadAllText((Join-Path $root "CHANGELOG.md"))
    if (-not [regex]::IsMatch($changelog, "^## \[$([regex]::Escape($Expected))\] - \d{4}-\d{2}-\d{2}", "Multiline")) {
        "CHANGELOG.md has no dated '## [$Expected] - YYYY-MM-DD' section"
        $failed = $true
    }
    if ($failed) { exit 1 }
    "All version references match $Expected."
}
