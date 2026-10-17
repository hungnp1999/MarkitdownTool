# Read-only CodeQL report for a pull request and/or a branch (default: main).
# Prints the analysis for the PR head, the analysis for the branch head (and the PR's merge commit
# once merged), any CodeQL runs still in progress, and every open alert, then a one-word verdict:
#   CLEAN    - analyses are current and nothing is open
#   ALERTS   - open alerts exist (exit 1)
#   PENDING  - an analysis for the relevant commit is still running or has not started (exit 2)
#   STALE    - the newest analysis is for an older commit and nothing is running (exit 2)
param(
    [int]$Pr,
    [string]$Branch = "main",
    [string]$Repo
)
$ErrorActionPreference = "Stop"

if (-not $Repo) { $Repo = (gh repo view --json nameWithOwner --jq .nameWithOwner).Trim() }
$api = "repos/$Repo"

function Get-Json([string]$path) {
    $raw = gh api $path 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($raw | Out-String | ConvertFrom-Json)
}

function Get-Pages([string]$path) {
    $items = @()
    for ($page = 1; $page -le 10; $page++) {
        $sep = $(if ($path.Contains("?")) { "&" } else { "?" })
        $batch = @(Get-Json "$path${sep}per_page=100&page=$page")
        $items += $batch
        if ($batch.Count -lt 100) { break }
    }
    return $items
}

$report = New-Object System.Collections.Generic.List[string]
function Say([string]$line) { $script:report.Add($line) }

function Short([string]$sha) { if ($sha) { $sha.Substring(0, 7) } else { "-" } }

function Get-LatestAnalysis([string]$ref) {
    $list = @(Get-Json "$api/code-scanning/analyses?ref=$ref&per_page=1")
    if ($list.Count -gt 0) { return $list[0] }
    return $null
}

function Get-CodeqlRuns([string]$sha) {
    $runs = Get-Json "$api/actions/runs?head_sha=$sha&per_page=50"
    if (-not $runs) { return @() }
    return @($runs.workflow_runs | Where-Object { $_.name -match "CodeQL" -or $_.path -match "codeql" })
}

function Show-Analysis([string]$label, $analysis, [string]$expectedSha) {
    if (-not $analysis) {
        Say ("  {0}: no analysis found" -f $label)
        return "missing"
    }
    $current = $analysis.commit_sha -eq $expectedSha
    $staleNote = $(if ($current) { "" } else { " - OLDER than head $(Short $expectedSha)" })
    $errorNote = $(if ($analysis.error) { " - ERROR: $($analysis.error)" } else { "" })
    Say ("  {0}: analysis {1} on {2} at {3}, {4} result(s){5}{6}" -f $label, $analysis.id, (Short $analysis.commit_sha), $analysis.created_at, $analysis.results_count, $staleNote, $errorNote)
    if ($current) { return "current" } else { return "stale" }
}

function Show-Runs([string]$sha) {
    $runs = Get-CodeqlRuns $sha
    foreach ($run in $runs) {
        Say ("    run: {0} [{1}/{2}] {3}" -f $run.name, $run.status, $(if ($run.conclusion) { $run.conclusion } else { "-" }), $run.html_url)
    }
    return @($runs | Where-Object { $_.status -ne "completed" }).Count -gt 0
}

function Show-Alerts([string]$label, [array]$alerts) {
    Say ("  {0}: {1} open" -f $label, $alerts.Count)
    foreach ($alert in ($alerts | Sort-Object { $_.number })) {
        $instance = $alert.most_recent_instance
        $severity = $(if ($alert.rule.security_severity_level) { $alert.rule.security_severity_level } else { $alert.rule.severity })
        $message = (($instance.message.text -split "`n")[0]).Trim()
        Say ("    #{0} [{1}] {2} {3}:{4} - {5} (since {6}) {7}" -f $alert.number, $severity, $alert.rule.id, $instance.location.path, $instance.location.start_line, $message, $alert.created_at.Substring(0, 10), $alert.html_url)
    }
}

$verdicts = New-Object System.Collections.Generic.List[string]

$setup = Get-Json "$api/code-scanning/default-setup"
if ($setup) {
    Say ("Code scanning default setup: {0}, languages: {1}, schedule: {2}" -f $setup.state, ($setup.languages -join ", "), $setup.schedule)
    Say ("")
}

if ($Pr) {
    $prInfo = gh pr view $Pr --repo $Repo --json number,title,state,headRefOid,baseRefName,mergeCommit,mergedAt | Out-String | ConvertFrom-Json
    Say ("PR #{0} [{1}] {2}" -f $prInfo.number, $prInfo.state, $prInfo.title)
    Say ("  head {0}, base {1}" -f (Short $prInfo.headRefOid), $prInfo.baseRefName)

    $checks = Get-Json "$api/commits/$($prInfo.headRefOid)/check-runs?per_page=100"
    foreach ($check in @($checks.check_runs | Where-Object { $_.name -eq "CodeQL" -or $_.name -like "Analyze*" })) {
        Say ("  check: {0} [{1}/{2}] {3}" -f $check.name, $check.status, $(if ($check.conclusion) { $check.conclusion } else { "-" }), $check.output.title)
        if ($check.status -eq "completed" -and $check.conclusion -notin @("success", "neutral", "skipped")) { $verdicts.Add("ALERTS") }
    }

    $state = Show-Analysis "PR head analysis" (Get-LatestAnalysis "refs/pull/$Pr/head") $prInfo.headRefOid
    $running = Show-Runs $prInfo.headRefOid
    if ($state -ne "current") { $verdicts.Add($(if ($running -or $state -eq "missing") { "PENDING" } else { "STALE" })) }

    $prAlerts = @(Get-Pages "$api/code-scanning/alerts?ref=refs/pull/$Pr/head&state=open")
    Show-Alerts "PR alerts (changed lines only)" $prAlerts
    if ($prAlerts.Count -gt 0) { $verdicts.Add("ALERTS") }

    if ($prInfo.mergeCommit -and $prInfo.mergeCommit.oid) {
        $mergeSha = $prInfo.mergeCommit.oid
        Say ("  merged {0} as {1}" -f $prInfo.mergedAt, (Short $mergeSha))
        # The analyses endpoint ignores ?sha=, so filter by commit here.
        $mergeAnalysis = @(Get-Pages "$api/code-scanning/analyses?ref=refs/heads/$($prInfo.baseRefName)") |
            Where-Object { $_.commit_sha -eq $mergeSha } | Select-Object -First 1
        if ($mergeAnalysis) {
            $null = Show-Analysis "$($prInfo.baseRefName) analysis of the merge commit" $mergeAnalysis $mergeSha
            # Alerts first raised by that analysis: what the merge introduced outside the PR's changed lines.
            $introduced = @(Get-Pages "$api/code-scanning/alerts?ref=refs/heads/$($prInfo.baseRefName)&state=all") |
                Where-Object { $_.created_at -eq $mergeAnalysis.created_at }
            Say ("  alerts first raised by the merge commit: {0}" -f @($introduced).Count)
            foreach ($alert in ($introduced | Sort-Object { $_.number })) {
                $location = $alert.most_recent_instance.location
                Say ("    #{0} {1} {2}:{3} - now {4}{5} {6}" -f $alert.number, $alert.rule.id, $location.path, $location.start_line, $alert.state,
                    $(if ($alert.fixed_at) { " ($($alert.fixed_at.Substring(0, 10)))" } elseif ($alert.dismissed_reason) { " ($($alert.dismissed_reason))" } else { "" }), $alert.html_url)
            }
        } else {
            Say ("  $($prInfo.baseRefName) analysis of the merge commit: none yet")
            $running = Show-Runs $mergeSha
            $verdicts.Add($(if ($running) { "PENDING" } else { "STALE" }))
        }
    }
    Say ("")
}

$head = (gh api "$api/commits/$Branch" --jq .sha).Trim()
Say ("Branch {0} at {1}" -f $Branch, (Short $head))
$state = Show-Analysis "$Branch analysis" (Get-LatestAnalysis "refs/heads/$Branch") $head
$running = Show-Runs $head
if ($state -ne "current") { $verdicts.Add($(if ($running) { "PENDING" } else { "STALE" })) }

$open = @(Get-Pages "$api/code-scanning/alerts?ref=refs/heads/$Branch&state=open")
Show-Alerts "$Branch alerts" $open
if ($open.Count -gt 0) { $verdicts.Add("ALERTS") }

Say ("")
$report | ForEach-Object { $_ }
if ($verdicts.Contains("ALERTS")) { "VERDICT: ALERTS"; exit 1 }
if ($verdicts.Contains("PENDING")) { "VERDICT: PENDING"; exit 2 }
if ($verdicts.Contains("STALE")) { "VERDICT: STALE"; exit 2 }
"VERDICT: CLEAN"
