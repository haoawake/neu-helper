<#
    NEU Helper uninstaller. ASCII-only, same reason as install.ps1.

    Usage:
        powershell -ExecutionPolicy Bypass -File uninstall.ps1
        powershell -ExecutionPolicy Bypass -File uninstall.ps1 -KeepToken

    Removes the logon entry, the MCP registration, and (unless -KeepToken) the
    stored credentials. Leaves the project files alone -- delete the folder
    yourself if you want those gone too.
#>
param([switch]$KeepToken)

$proj = $PSScriptRoot
function Ok($msg)   { Write-Host "    OK   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    WARN $msg" -ForegroundColor Yellow }

Write-Host ""
Write-Host "=== NEU Helper uninstaller ===" -ForegroundColor White

# 1. logon shortcut
# Both names: anything installed before the rename is still on disk
$gone = $false
foreach ($n in @("NEU Helper.lnk", "Canvas Helper.lnk")) {
    $p = Join-Path ([Environment]::GetFolderPath("Startup")) $n
    if (Test-Path $p) { Remove-Item $p -Force; Ok "removed logon shortcut ($n)"; $gone = $true }
    $p = Join-Path ([Environment]::GetFolderPath("Desktop")) $n
    if (Test-Path $p) { Remove-Item $p -Force; Ok "removed desktop shortcut ($n)"; $gone = $true }
}
if (-not $gone) { Warn "no shortcuts found" }

# 2. MCP registration
if (Get-Command claude -ErrorAction SilentlyContinue) {
    # PS 5.1 turns native stderr into ErrorRecords; judge by exit code only.
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    (& claude mcp remove canvas -s user 2>&1) | Out-Null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -eq 0) { Ok "unregistered the canvas MCP server" } else { Warn "canvas MCP server was not registered" }
} else {
    Warn "claude CLI not on PATH; skipped MCP cleanup"
}

# 3. credentials
if ($KeepToken) {
    Warn "kept the stored token (-KeepToken)"
} else {
    $cfgFile = Join-Path $env:USERPROFILE ".canvas-helper\config.json"
    if (Test-Path $cfgFile) {
        Remove-Item $cfgFile -Force
        Ok "deleted $cfgFile"
    }
    [Environment]::SetEnvironmentVariable("CANVAS_API_TOKEN", $null, "User")
    [Environment]::SetEnvironmentVariable("CANVAS_BASE_URL", $null, "User")
    Ok "cleared the user env vars"
    Write-Host ""
    Write-Host "  Reminder: this does NOT revoke the token on Canvas." -ForegroundColor Yellow
    Write-Host "  Delete it at: Account -> Settings -> Approved Integrations" -ForegroundColor Yellow
}

# 4. local cache
$dataDir = Join-Path $proj "data"
if (Test-Path $dataDir) {
    Remove-Item (Join-Path $dataDir "*") -Force -Recurse -ErrorAction SilentlyContinue
    Ok "cleared data/ cache"
}

Write-Host ""
Write-Host "=== Done. Project files left in place. ===" -ForegroundColor White
Write-Host ""
