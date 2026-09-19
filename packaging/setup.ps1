<#
    NEU Helper -- first-run setup for the packaged (no-Python) build.

    Run this once after unzipping:

        powershell -ExecutionPolicy Bypass -File setup.ps1 -Token "14523~...."

    Re-running is safe; it reuses the stored token if you omit -Token.

    This is the packaged counterpart of install.ps1. Two differences, both
    because there is no Python interpreter in this build:

      * the MCP server is registered as canvas-mcp.exe, not `python canvas_mcp.py`
      * there is no once-per-day logon gate (that gate is a .vbs that launches
        a Python script). The startup shortcut simply runs the app at every
        logon -- harmless, because the app allows only one instance and the
        daily briefing is separately capped at one per calendar day.

    ASCII-only on purpose: Windows decodes .ps1 with the system ANSI code page,
    so non-ASCII here would come out as mojibake on a Chinese system.
#>
param(
    [string]$Token = "",
    [string]$BaseUrl = "https://northeastern.instructure.com"
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$exe = Join-Path $here "NEU Helper.exe"
$mcpExe = Join-Path $here "canvas-mcp.exe"

function Ok($m)   { Write-Host "    OK   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    WARN $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "    FAIL $m" -ForegroundColor Red }
function Step($n, $m) { Write-Host ""; Write-Host "[$n] $m" -ForegroundColor Cyan }

Write-Host ""
Write-Host "=== NEU Helper setup (packaged build) ===" -ForegroundColor White
Write-Host "    folder: $here"

if (-not (Test-Path $exe)) {
    Fail "NEU Helper.exe is not next to this script. Unzip the whole folder first."
    exit 1
}
if (-not (Test-Path $mcpExe)) {
    Fail "canvas-mcp.exe is not next to this script. Re-extract the whole release zip."
    exit 1
}

# ---------------------------------------------------------------- 0. unblock
Step 0 "Clearing the download tag"
# Windows marks every file extracted from a downloaded zip as "from the
# internet" (a Zone.Identifier stream), and then refuses to load DLLs and .NET
# assemblies that carry it. The exes here are single-file builds so they cope
# on their own, but clear the whole folder anyway -- it costs nothing and it
# means nothing in here can ever hit that wall.
try {
    Get-ChildItem -LiteralPath $here -Recurse -File -ErrorAction Stop | Unblock-File
    Ok "cleared the 'downloaded from the internet' tag on this folder"
} catch {
    Warn "could not clear the download tag: $($_.Exception.Message)"
}

# ---------------------------------------------------------------- 1. token
Step 1 "Storing the Canvas token"
$cfgDir = Join-Path $env:USERPROFILE ".canvas-helper"
$cfgFile = Join-Path $cfgDir "config.json"

if (-not $Token) {
    if (Test-Path $cfgFile) {
        $existing = Get-Content $cfgFile -Raw | ConvertFrom-Json
        $Token = $existing.token
        if ($existing.base_url) { $BaseUrl = $existing.base_url }
        Ok "reusing the token already in $cfgFile"
    } else {
        Fail 'no token yet. Re-run with:  -Token "14523~...."'
        Write-Host "         Get one at: Canvas -> Account -> Settings -> New Access Token"
        exit 1
    }
}

if (-not (Test-Path $cfgDir)) { New-Item -ItemType Directory -Path $cfgDir | Out-Null }
if (Test-Path $cfgFile) { Remove-Item $cfgFile -Force }
$json = @{ token = $Token; base_url = $BaseUrl } | ConvertTo-Json
[IO.File]::WriteAllText($cfgFile, $json, (New-Object Text.UTF8Encoding $false))

# Grant by SID, not by name: when the machine name equals the user name,
# "$env:USERNAME:(R,W)" is read as a domain prefix and grants nothing.
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $cfgFile /inheritance:r /grant:r "*${sid}:(R,W)" | Out-Null
if ($LASTEXITCODE -eq 0) { Ok "token -> $cfgFile (readable only by you)" }
else { Warn "token written, but locking down its permissions failed" }

# ---------------------------------------------------------------- 2. MCP
Step 2 "Registering the canvas MCP server with Claude Code"
$mcpRegistered = $false
$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $claude) {
    foreach ($c in @("$env:USERPROFILE\.local\bin\claude.exe",
                     "$env:LOCALAPPDATA\Programs\claude\claude.exe")) {
        if (Test-Path $c) { $claude = $c; break }
    }
}
if (-not $claude) {
    Warn "claude CLI not found. Canvas, mailbox, and memos will still work."
    Warn "AI chat, briefings, mail AI, and Canvas MCP are NOT configured yet."
    Warn "Install and log in to Claude Code, then run this setup.ps1 again."
    Warn "The saved Canvas token will be reused; do not pass -Token again."
} else {
    # PowerShell 5.1 can promote a native program's stderr to a terminating
    # ErrorRecord under Stop. Judge these commands by their exit codes instead.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $claude mcp remove canvas -s user 2>&1 | Out-Null
    # Point at the bundled exe -- this build has no Python to run canvas_mcp.py
    & $claude mcp add canvas -s user -- $mcpExe 2>&1 | Out-Null
    $mcpAddCode = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($mcpAddCode -eq 0) {
        $mcpRegistered = $true
        Ok "MCP server 'canvas' -> canvas-mcp.exe"
    } else {
        Warn "claude mcp add failed. After fixing Claude Code, run setup.ps1 again."
    }
}

# ---------------------------------------------------------------- 3. config
Step 3 "Preparing the assistant's config"
$settingsDir = Join-Path $here ".claude"
if (-not (Test-Path $settingsDir)) { New-Item -ItemType Directory -Path $settingsDir | Out-Null }
$settings = @'
{
  "permissions": {
    "allow": [
      "mcp__canvas",
      "Read(./data/**)",
      "Skill(course-files)",
      "Skill(course-sync)",
      "Bash(python .claude/skills/course-files/scripts/coursedoc.py:*)",
      "Bash(python3 .claude/skills/course-files/scripts/coursedoc.py:*)",
      "Bash(python .claude/skills/course-sync/scripts/sync_now.py:*)",
      "Bash(python3 .claude/skills/course-sync/scripts/sync_now.py:*)",
      "PowerShell(python .claude/skills/course-files/scripts/coursedoc.py:*)",
      "PowerShell(python .claude/skills/course-sync/scripts/sync_now.py:*)"
    ]
  }
}
'@
[IO.File]::WriteAllText((Join-Path $settingsDir "settings.local.json"), $settings,
                        (New-Object Text.UTF8Encoding $false))
Ok "wrote .claude\settings.local.json"

# CLAUDE.md is the assistant's behaviour definition: course table, briefing
# rules, boundaries. It must sit next to the exe, because the app runs
# `claude -p` with this folder as the working directory.
$claudeMd = Join-Path $here "CLAUDE.md"
$claudeTpl = Join-Path $here "CLAUDE.example.md"
if ((Test-Path $claudeTpl) -and -not (Test-Path $claudeMd)) {
    Copy-Item $claudeTpl $claudeMd
    Ok "created CLAUDE.md -- open it and put your own courses in the table"
} elseif (Test-Path $claudeMd) {
    Ok "CLAUDE.md already there (left alone)"
}

# ---------------------------------------------------------------- 4. shortcuts
Step 4 "Creating shortcuts"
$ws = New-Object -ComObject WScript.Shell
foreach ($spec in @(
    @{ Dir = [Environment]::GetFolderPath("Desktop"); What = "desktop" },
    @{ Dir = [Environment]::GetFolderPath("Startup"); What = "logon" }
)) {
    $lnk = $ws.CreateShortcut((Join-Path $spec.Dir "NEU Helper.lnk"))
    $lnk.TargetPath = $exe
    $lnk.WorkingDirectory = $here
    $lnk.IconLocation = $exe
    $lnk.Description = "NEU Helper"
    $lnk.Save()
    Ok "$($spec.What) shortcut created"
}
Ok "the app allows only one instance, so extra launches just focus the window"

# ---------------------------------------------------------------- 5. verify
Step 5 "Verifying"

# Talk to the MCP server the way Claude Code does: one initialize request on
# stdin, expect a JSON-RPC result back. It exits by itself when stdin closes,
# so nothing is left running. (There is no --selftest flag; it is a stdio
# server, and calling it with a flag would just block waiting for input.)
$init = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"setup","version":"1"}}}'
$whoami = '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"canvas_whoami","arguments":{}}}'

# The server writes progress notes to stderr. Two reasons to swallow them here:
# they are noise in an installer log, and PowerShell 5.1 wraps native stderr in
# ErrorRecords -- with $ErrorActionPreference = "Stop" that aborts the script on
# a line that is not even an error. So relax the preference and send stderr to
# $null for these two calls only.
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$reply = $init | & $mcpExe 2>$null
$reply2 = ($init + "`n" + $whoami) | & $mcpExe 2>$null
$ErrorActionPreference = $prev

if ($reply -match '"serverInfo"') { Ok "canvas-mcp.exe answers the MCP handshake" }
else { Warn "canvas-mcp.exe did not answer as expected" }

if ($reply2 -match '"result"' -and $reply2 -notmatch '"isError":true') {
    Ok "Canvas token works (canvas_whoami came back)"
} else {
    Warn "Canvas check failed -- is the token still valid?"
}

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor White
if ($mcpRegistered) {
    Write-Host "    AI setup:  Canvas MCP registered. Run 'claude mcp list' to verify it." -ForegroundColor Green
    Write-Host "               Also run 'claude' once to confirm the account can answer." -ForegroundColor Gray
} else {
    Write-Host "    AI setup:  INCOMPLETE. The app works, but AI features are not ready." -ForegroundColor Yellow
    Write-Host "               Install/login to Claude Code, then re-run:" -ForegroundColor Yellow
    Write-Host "               powershell -ExecutionPolicy Bypass -File setup.ps1" -ForegroundColor Yellow
}
Write-Host "    Start it:  double-click 'NEU Helper' on the Desktop"
Write-Host "    Mailbox:   set it up inside the app, Settings -> mailbox"
Write-Host "    Your token lives in $cfgDir, outside this folder."
Write-Host ""
