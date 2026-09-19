<#
    NEU Helper installer.

    ASCII-only on purpose: Windows PowerShell 5.1 decodes .ps1 files with the
    system ANSI codepage unless they carry a BOM, so CJK text here would be
    mojibake. Chinese output lives in brief.py / CLAUDE.md, read as UTF-8.

    Usage:
        powershell -ExecutionPolicy Bypass -File install.ps1 -Token "14523~...."

    Re-running is safe; it reuses the stored token if -Token is omitted.

    What it does:
        1. stores the token in %USERPROFILE%\.canvas-helper\config.json (locked ACL)
        2. sets user env vars CANVAS_API_TOKEN / CANVAS_BASE_URL
        3. registers the "canvas" MCP server with Claude Code at user scope
        4. pre-approves the canvas MCP tools for this project
        5. drops a hidden logon shortcut that opens the desktop app
        6. verifies the whole chain end to end
#>
param(
    [string]$Token = "",
    [string]$BaseUrl = "https://northeastern.instructure.com"
)

$ErrorActionPreference = "Stop"
$proj = $PSScriptRoot
$cfgDir = Join-Path $env:USERPROFILE ".canvas-helper"
$cfgFile = Join-Path $cfgDir "config.json"

function Step($n, $msg) { Write-Host ""; Write-Host "[$n] $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    OK   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    WARN $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "    FAIL $msg" -ForegroundColor Red }

function Native {
    <#
        Run a native .exe and capture output + exit code.

        PS 5.1 wraps each stderr line from a native command in an ErrorRecord,
        which under $ErrorActionPreference='Stop' aborts the script even when
        the exe exited 0. So drop to 'Continue' around the call and judge
        success by $LASTEXITCODE instead.
    #>
    param([string]$File, [string[]]$ArgList)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = (& $File @ArgList 2>&1 | Out-String)
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    return [pscustomobject]@{ Out = $out.Trim(); Code = $code }
}

function WriteUtf8NoBom($path, $text) {
    # Set-Content -Encoding utf8 on PS 5.1 emits a BOM, and Python's
    # json.loads chokes on it. Write the bytes ourselves.
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

Write-Host ""
Write-Host "=== NEU Helper installer ===" -ForegroundColor White
Write-Host "    project: $proj"

# ---------------------------------------------------------------- 1. token
Step 1 "Storing credentials"

if (-not $Token) {
    if (Test-Path $cfgFile) {
        $existing = Get-Content $cfgFile -Raw | ConvertFrom-Json
        $Token = $existing.token
        if ($existing.base_url) { $BaseUrl = $existing.base_url }
        Ok "reusing token already in $cfgFile"
    } else {
        Fail "no token found. Pass -Token ""14523~...."" on the command line."
        exit 1
    }
}

if (-not (Test-Path $cfgDir)) { New-Item -ItemType Directory -Path $cfgDir | Out-Null }
# Recreate so the ACL below starts from a clean slate.
if (Test-Path $cfgFile) { Remove-Item $cfgFile -Force }
WriteUtf8NoBom $cfgFile (@{ token = $Token; base_url = $BaseUrl } | ConvertTo-Json)

# Grant by SID, not by name. "$env:USERNAME:(R,W)" is ambiguous when the
# machine name equals the user name -- icacls then reads the name as a domain
# prefix and grants nothing, locking the file out entirely.
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$acl = Native "icacls.exe" @($cfgFile, "/inheritance:r", "/grant:r", "*${sid}:(R,W)")
if ($acl.Code -eq 0) { Ok "token -> $cfgFile (readable only by SID $sid)" }
else { Warn "token written, but ACL lockdown failed: $($acl.Out)" }

[Environment]::SetEnvironmentVariable("CANVAS_API_TOKEN", $Token, "User")
[Environment]::SetEnvironmentVariable("CANVAS_BASE_URL", $BaseUrl, "User")
$env:CANVAS_API_TOKEN = $Token
$env:CANVAS_BASE_URL = $BaseUrl
Ok "user env vars CANVAS_API_TOKEN / CANVAS_BASE_URL set"

# ---------------------------------------------------------------- 2. python
Step 2 "Locating Python"
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { Fail "python not on PATH. Install Python 3.9+ and re-run."; exit 1 }

$ver = Native $py @("-c", "import sys;print('.'.join(map(str,sys.version_info[:3])))")
if ($ver.Code -ne 0) { Fail "cannot run $py"; exit 1 }
Ok "python $($ver.Out) at $py"

# requests = Canvas API, flask = the local HTTP backend, pywebview = the window.
foreach ($pkg in @("requests", "flask", "pywebview")) {
    $probe = Native $py @("-c", "from importlib.metadata import version; print(version('$pkg'))")
    if ($probe.Code -ne 0) {
        Warn "$pkg is missing; installing it"
        $pip = Native $py @("-m", "pip", "install", "--quiet", $pkg)
        if ($pip.Code -ne 0) { Fail "pip install $pkg failed: $($pip.Out)"; exit 1 }
        Ok "$pkg installed"
    } else {
        Ok "$pkg $($probe.Out)"
    }
}

# Record the absolute interpreter path for startup-gate.vbs, because PATH at
# logon is not always the same PATH an interactive shell sees.
#
# pythonw.exe, because the gate must launch with a normal (not hidden) window
# style -- a hidden style would hide the app's own window too. See the header
# comment in startup-gate.vbs for the full reasoning.
$dataDir = Join-Path $proj "data"
if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir | Out-Null }
$pyw = Join-Path (Split-Path $py -Parent) "pythonw.exe"
if (Test-Path $pyw) {
    WriteUtf8NoBom (Join-Path $dataDir "pythonw-path.txt") $pyw
    Ok "logon interpreter: $pyw"
} else {
    Warn "pythonw.exe not found beside python.exe; a console window will show at logon"
    WriteUtf8NoBom (Join-Path $dataDir "pythonw-path.txt") $py
}
Remove-Item (Join-Path $dataDir "python-path.txt") -ErrorAction SilentlyContinue

# WebView2 renders the UI. Windows 11 ships it, but check rather than assume.
$wv = "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
if (Test-Path $wv) {
    Ok "WebView2 runtime $((Get-ItemProperty $wv).pv)"
} else {
    Warn "WebView2 runtime not detected; install it from https://go.microsoft.com/fwlink/p/?LinkId=2124703"
}

# ---------------------------------------------------------------- 3. MCP
Step 3 "Registering the canvas MCP server with Claude Code"
$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $claude) {
    Warn "claude CLI not on PATH; skipping MCP registration"
} else {
    $mcpScript = Join-Path $proj "canvas_mcp.py"
    # Clear any earlier registration; a missing one is not an error here.
    Native $claude @("mcp", "remove", "canvas", "-s", "user") | Out-Null
    $add = Native $claude @("mcp", "add", "canvas", "-s", "user", "--", $py, $mcpScript)
    if ($add.Code -eq 0) { Ok "MCP server 'canvas' registered at user scope (all projects)" }
    else { Fail "claude mcp add failed: $($add.Out)" }
}

# ---------------------------------------------------------------- 4. perms
Step 4 "Pre-approving canvas tools for this project"
$settingsDir = Join-Path $proj ".claude"
$settingsFile = Join-Path $settingsDir "settings.local.json"
if (-not (Test-Path $settingsDir)) { New-Item -ItemType Directory -Path $settingsDir | Out-Null }

# Without this, every boot would nag for permission on each canvas tool call.
# The Skill/Bash entries matter just as much: the desktop app talks to
# `claude -p` (non-interactive), which cannot prompt -- anything that would need
# confirmation is denied outright, so the course-files / course-sync skills
# would silently never run. Only those two script prefixes are allowed, not Bash
# in general.
#
# python AND python3 are both allowed: macOS usually has only python3,
# and this file is shared between both machines -- listing both keeps one
# file correct everywhere instead of the two installers overwriting each
# other. Keep this list identical to the one in install.sh.
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
WriteUtf8NoBom $settingsFile $settings
Ok "wrote $settingsFile"

# CLAUDE.md is the assistant's behaviour definition: course table, briefing
# rules, boundaries. It is NOT in the repository -- it holds your own courses
# and situation -- so a fresh clone does not have one. Seed it from the
# template; never overwrite an existing one.
$claudeMd = Join-Path $proj "CLAUDE.md"
$claudeTpl = Join-Path $proj "CLAUDE.example.md"
if ((Test-Path $claudeTpl) -and -not (Test-Path $claudeMd)) {
    Copy-Item $claudeTpl $claudeMd
    Ok "created CLAUDE.md from the template -- edit the course table in it"
} elseif (Test-Path $claudeMd) {
    Ok "CLAUDE.md already there (left alone)"
}

# ---------------------------------------------------------------- 5. startup
Step 5 "Installing the logon entry"
$startup = [Environment]::GetFolderPath("Startup")
$lnkPath = Join-Path $startup "NEU Helper.lnk"
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = Join-Path $env:SystemRoot "System32\wscript.exe"
$lnk.Arguments = '"' + (Join-Path $proj "startup-gate.vbs") + '"'
$lnk.WorkingDirectory = $proj
$lnk.Description = "Open the Canvas study assistant at logon"
$lnk.WindowStyle = 7
$lnk.Save()
Ok "shortcut -> $lnkPath"
Ok "gate: opens at most one window per day (edit oncePerDay in startup-gate.vbs)"

# A desktop shortcut as well -- the logon gate refuses to open twice in one day,
# so without this there is no obvious way to start the app by hand.
$icon = Join-Path $proj "gui\icon.ico"
if (-not (Test-Path $icon)) {
    Native { & $py (Join-Path $proj "make_icon.py") } "generate icon"
}
$deskPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "NEU Helper.lnk"
$desk = $ws.CreateShortcut($deskPath)
$desk.TargetPath = $pyw
$desk.Arguments = '"' + (Join-Path $proj "app.py") + '"'
$desk.WorkingDirectory = $proj
$desk.Description = "NEU Helper"
if (Test-Path $icon) { $desk.IconLocation = $icon }
$desk.WindowStyle = 1
$desk.Save()
Ok "desktop shortcut -> $deskPath"

# ---------------------------------------------------------------- 6. verify
Step 6 "Verifying"

Push-Location $proj
$env:PYTHONUTF8 = "1"

$probe = Native $py @("-c", "from canvas_api import CanvasClient; print(CanvasClient().whoami()['name'])")
if ($probe.Code -eq 0) { Ok "Canvas API reachable, logged in as: $($probe.Out)" }
else { Fail "Canvas API probe failed: $($probe.Out)" }

$handshake = @'
import json, subprocess, sys
msgs = [
    {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"install","version":"1"}}},
    {"jsonrpc":"2.0","method":"notifications/initialized"},
    {"jsonrpc":"2.0","id":2,"method":"tools/list"},
]
p = subprocess.run([sys.executable, "canvas_mcp.py"],
                   input="\n".join(json.dumps(m) for m in msgs) + "\n",
                   capture_output=True, text=True, encoding="utf-8", timeout=90)
names = []
for line in p.stdout.splitlines():
    r = json.loads(line)
    if r.get("id") == 2:
        names = [t["name"] for t in r["result"]["tools"]]
if not names:
    print("NO TOOLS. stderr:", p.stderr[:500]); sys.exit(1)
print(len(names), "tools:", ", ".join(names))
'@
$hsFile = Join-Path $env:TEMP "canvas-helper-handshake.py"
WriteUtf8NoBom $hsFile $handshake
$hs = Native $py @($hsFile)
if ($hs.Code -eq 0) { Ok "MCP stdio handshake -> $($hs.Out)" }
else { Fail "MCP handshake failed: $($hs.Out)" }
Remove-Item $hsFile -ErrorAction SilentlyContinue

# Exercise the desktop app's own HTTP backend, minus the window.
$smoke = @'
import json, os, sys, threading, time, urllib.error, urllib.request

# This file lives in %TEMP%, and Python puts the *script's* directory on
# sys.path -- not the cwd -- so "import server" would miss. We are Push-Location'd
# into the project, so the cwd is the right place to look.
sys.path.insert(0, os.getcwd())
import server

port = server.free_port()
threading.Thread(target=server.serve, args=(port,), daemon=True).start()
base = "http://127.0.0.1:%d" % port

for _ in range(120):
    try:
        urllib.request.urlopen("%s/api/dashboard?k=%s" % (base, server.TOKEN), timeout=3).read(1)
        break
    except urllib.error.HTTPError:
        break
    except Exception:
        time.sleep(0.2)

# the token must actually gate /api
try:
    urllib.request.urlopen("%s/api/dashboard" % base, timeout=5)
    raise SystemExit("FAIL: /api is reachable without a token")
except urllib.error.HTTPError as e:
    if e.code != 403:
        raise SystemExit("FAIL: expected 403 without token, got %d" % e.code)

# but the static shell must load, or the UI renders unstyled
for path in ("/", "/app.css", "/app.js"):
    r = urllib.request.urlopen(base + path, timeout=5)
    if r.status != 200:
        raise SystemExit("FAIL: %s returned %d" % (path, r.status))

d = json.load(urllib.request.urlopen("%s/api/dashboard?k=%s" % (base, server.TOKEN), timeout=90))
if d.get("error"):
    raise SystemExit("FAIL: dashboard error %s: %s" % (d["error"], d.get("message")))
print("%d courses, %d todo, %d announcements" % (
    len(d["courses"]), len(d["todo"]), len(d["announcements"])))
'@
$smokeFile = Join-Path $env:TEMP "canvas-helper-smoke.py"
WriteUtf8NoBom $smokeFile $smoke
$sm = Native $py @($smokeFile)
if ($sm.Code -eq 0) { Ok "desktop backend: $($sm.Out)" }
else { Fail "desktop backend check failed: $($sm.Out)" }
Remove-Item $smokeFile -ErrorAction SilentlyContinue

Pop-Location

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor White
Write-Host ""
Write-Host "  Next boot:  the NEU Helper window opens by itself." -ForegroundColor Gray
Write-Host "  Try it now: python app.py          (the desktop app)" -ForegroundColor Gray
Write-Host "  Terminal:   .\start-canvas-helper.cmd   (text fallback)" -ForegroundColor Gray
Write-Host "  Anywhere:   run 'claude' and just ask about your courses." -ForegroundColor Gray
Write-Host "  Remove:     .\uninstall.ps1" -ForegroundColor Gray
Write-Host ""
Write-Host "  NOTE: already-running Claude Code sessions will not see the new" -ForegroundColor Yellow
Write-Host "        MCP server until you restart them." -ForegroundColor Yellow
Write-Host ""
