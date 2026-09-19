<#
    Build the packaged Windows release.

        powershell -ExecutionPolicy Bypass -File packaging\build_win.ps1

    Produces dist\NEU-Helper-win-x64.zip. For AI features, install and log in
    to Claude Code before running setup.ps1.

    Three things this script does that the spec cannot:

      1. copies CLAUDE.example.md and .claude\skills NEXT TO the exe. They must
         not go inside _internal: the app runs `claude -p` with the exe's folder
         as the working directory, so that is where Claude Code looks for them.
      2. copies setup.ps1 and a short README.txt into the folder
      3. zips it

    ASCII-only, same reason as install.ps1.
#>
$ErrorActionPreference = "Stop"
$proj = Split-Path $PSScriptRoot -Parent
Push-Location $proj

function Say($m) { Write-Host "  $m" -ForegroundColor Cyan }

# The icon is generated from the same code that draws the floating orb, so the
# two never drift. Cheap to regenerate; always do it.
Say "generating the icon"
& python make_icon.py --ico
if ($LASTEXITCODE -ne 0) { throw "make_icon.py failed" }

Say "running PyInstaller (a few minutes)"
Remove-Item -Recurse -Force dist, build -ErrorAction SilentlyContinue
& python -m PyInstaller packaging\neu-helper.spec --noconfirm --distpath dist --workpath build
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# onefile: PyInstaller drops the two exes straight into dist\, so assemble
# the distribution folder here.
foreach ($n in @("NEU Helper.exe", "canvas-mcp.exe")) {
    if (-not (Test-Path (Join-Path $proj "dist\$n"))) { throw "$n was not produced" }
}
$out = Join-Path $proj "dist\NEU Helper"
New-Item -ItemType Directory -Path $out | Out-Null
Move-Item (Join-Path $proj "dist\NEU Helper.exe") $out
Move-Item (Join-Path $proj "dist\canvas-mcp.exe") $out

Say "copying the files that must sit next to the exe"
Copy-Item (Join-Path $proj "CLAUDE.example.md") $out
Copy-Item (Join-Path $proj "packaging\setup.ps1") $out
Copy-Item -Recurse (Join-Path $proj ".claude\skills") (Join-Path $out ".claude\skills")

$readme = @"
NEU Helper -- packaged build for Windows (no Python needed)

  1. If you want AI chat, briefings, mail AI, or Canvas MCP, install and
     log in to Claude Code first:

       irm https://claude.ai/install.ps1 | iex
       claude --version
       claude

     Claude Code requires a supported subscription/account or API billing.
     Canvas, mailbox, and memos can work without it.

  2. Unzip this whole folder somewhere you will keep it
     (it writes data\ next to the exe: mail, chats, course files, settings).

  3. Run setup once, with a Canvas access token:

       powershell -ExecutionPolicy Bypass -File setup.ps1 -Token "14523~...."

     Get a token at: Canvas -> Account -> Settings -> New Access Token.

  4. Open CLAUDE.md next to the exe and replace the example course table.
     The course_id is the number at the end of a Canvas course URL.

  5. Double-click "NEU Helper" on the Desktop.

Verify the complete setup
  - NEU Helper shows your real Canvas courses and assignments
  - `claude` can answer a normal question
  - `claude mcp list` shows `canvas`
  - asking Claude "What courses are in my Canvas?" returns real data

If Claude Code was missing during setup
  Install and log in to Claude Code, then run this again:

    powershell -ExecutionPolicy Bypass -File setup.ps1

  Your stored Canvas token is reused; do not pass it again.

Manual MCP fallback for THIS packaged build
  Run these commands from this folder (no Python needed):

    `$Mcp = (Resolve-Path ".\canvas-mcp.exe").Path
    claude mcp remove canvas -s user
    claude mcp add canvas -s user -- "`$Mcp"

Why setup.ps1 and not just the exe
  Windows tags every file that came out of a downloaded zip as "from the
  internet", and refuses to load DLLs that carry that tag. The two exes here
  are single-file builds, so only the exe itself is tagged and Windows is
  happy -- but setup.ps1 clears the tag from the whole folder anyway, so the
  helper files are clean too. (An earlier multi-file build failed to start for
  exactly this reason.)

What setup.ps1 does
  - stores the token in %USERPROFILE%\.canvas-helper\ (outside this folder,
    locked to your account)
  - registers canvas-mcp.exe with Claude Code, so you can ask about your
    courses from any Claude Code session
  - creates a Desktop and a logon shortcut
  - copies CLAUDE.example.md to CLAUDE.md -- EDIT IT and put your own courses
    in the table, otherwise the daily briefing talks about the wrong classes

Needs separately
  - Claude Code CLI, for the daily briefing and the chat:
    https://code.claude.com/docs/en/overview
    (the dashboard, the mailbox and the memos work without it)
  - WebView2 runtime, which Windows 11 already ships

Not in this build
  - the two course-file skills shell out to `python`, which this build does
    not carry. Searching course materials still works (Claude greps the .txt
    files that sync produces); only the helper scripts are unavailable.
    Install Python and use the source version if you want them.

Source, issues, the macOS version:
  https://github.com/haoawake/neu-helper
"@
[IO.File]::WriteAllText((Join-Path $out "README.txt"), $readme,
                        (New-Object Text.UTF8Encoding $false))

Say "zipping"
$zip = Join-Path $proj "dist\NEU-Helper-win-x64.zip"
Remove-Item $zip -ErrorAction SilentlyContinue
Compress-Archive -Path $out -DestinationPath $zip -CompressionLevel Optimal

$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host ""
Write-Host "  done -> $zip  ($mb MB)" -ForegroundColor Green
Pop-Location
