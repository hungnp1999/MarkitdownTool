# Prints the "## SHA-256" block for the release notes, in the order earlier releases used.
# With -OutFile, also writes the block to that file (UTF-8, no BOM).
param(
    [string]$OutFile
)
$ErrorActionPreference = "Stop"

$dist = Join-Path (git rev-parse --show-toplevel).Trim() "dist"
$lines = @("## SHA-256", "")
foreach ($name in @("MarkitdownTool-Setup.exe", "markitdown_gui.exe", "markitdowntool.exe")) {
    $path = Join-Path $dist $name
    if (-not (Test-Path $path)) { throw "Missing $path" }
    $lines += "- ${name}: $((Get-FileHash $path -Algorithm SHA256).Hash.ToUpperInvariant())"
}

$block = ($lines -join "`n") + "`n"
if ($OutFile) { [IO.File]::WriteAllText($OutFile, $block, (New-Object Text.UTF8Encoding($false))) }
$block
