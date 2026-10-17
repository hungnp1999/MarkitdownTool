# Checks the three release binaries in dist\ before they are published:
# they exist and are fresh, report the expected version, the console exe converts a real file,
# the GUI exe starts and stays up, and the installer carries the right ProductVersion.
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [datetime]$BuiltAfter
)
$ErrorActionPreference = "Stop"

$root = (git rev-parse --show-toplevel).Trim()
$dist = Join-Path $root "dist"
$cli = Join-Path $dist "markitdowntool.exe"
$gui = Join-Path $dist "markitdown_gui.exe"
$setup = Join-Path $dist "MarkitdownTool-Setup.exe"
$results = New-Object System.Collections.Generic.List[object]

function Add-Result([string]$check, [bool]$passed, [string]$detail) {
    $results.Add([pscustomobject]@{ Check = $check; Result = $(if ($passed) { "PASS" } else { "FAIL" }); Detail = $detail })
}

foreach ($file in @($setup, $gui, $cli)) {
    $name = Split-Path -Leaf $file
    if (-not (Test-Path $file)) { Add-Result "$name exists" $false "missing"; continue }
    $item = Get-Item $file
    $detail = "{0:N1} MB, built {1:yyyy-MM-dd HH:mm}" -f ($item.Length / 1MB), $item.LastWriteTime
    $fresh = (-not $BuiltAfter) -or ($item.LastWriteTime -ge $BuiltAfter)
    if (-not $fresh) { $detail += " (older than this build - stale file)" }
    Add-Result "$name exists" $fresh $detail
}

if (Test-Path $cli) {
    $versionLine = ((& $cli --version) | Out-String).Trim()
    Add-Result "markitdowntool --version" ($versionLine -eq "MarkitdownTool $Version") $versionLine

    $info = (& $cli info --json) | Out-String | ConvertFrom-Json
    Add-Result "markitdowntool info --json" ($LASTEXITCODE -eq 0 -and $info.version -eq $Version) "version=$($info.version), tesseract=$($info.tesseract)"

    $work = Join-Path ([IO.Path]::GetTempPath()) ("markitdowntool-smoke-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $work | Out-Null
    try {
        $sample = Join-Path $work "smoke.xlsx"
        $python = Join-Path $root ".venv\Scripts\python.exe"
        & $python -c "import openpyxl, sys; wb = openpyxl.Workbook(); wb.active.append(['smoke', 'test']); wb.save(sys.argv[1])" $sample
        $output = Join-Path $work "out"
        & $cli convert $sample --mode markdown --output-dir $output --json --quiet | Out-Null
        $exitCode = $LASTEXITCODE
        $markdown = Get-ChildItem $output -Recurse -Filter *.md -ErrorAction SilentlyContinue | Select-Object -First 1
        $converted = $exitCode -eq 0 -and $markdown -and ((Get-Content $markdown.FullName -Raw) -match "smoke")
        Add-Result "markitdowntool convert (xlsx -> md)" ([bool]$converted) "exit $exitCode, output $(if ($markdown) { $markdown.Name } else { 'none' })"
    } finally {
        Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if (Test-Path $gui) {
    # Onefile exes unpack before the window appears; give it time, then kill the whole process tree.
    $process = Start-Process $gui -PassThru
    Start-Sleep -Seconds 10
    $alive = -not $process.HasExited
    $detail = $(if ($alive) { "running after 10 s" } else { "exited with code $($process.ExitCode)" })
    if ($alive) { taskkill /PID $process.Id /T /F | Out-Null }
    Add-Result "markitdown_gui.exe starts" $alive $detail
}

if (Test-Path $setup) {
    $productVersion = (Get-Item $setup).VersionInfo.ProductVersion
    Add-Result "Setup ProductVersion" ($productVersion -eq $Version) "$productVersion"
}

$results | Format-Table -AutoSize | Out-String -Width 200
if ($results | Where-Object { $_.Result -eq "FAIL" }) {
    "SMOKE TEST FAILED"
    exit 1
}
"All smoke checks passed for $Version."
