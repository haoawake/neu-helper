<#
    Build the packaged Windows release.

        powershell -ExecutionPolicy Bypass -File packaging\build_win.ps1

    Produces  dist\NEU-Helper-win-x64.zip  -- unzip anywhere, run setup.ps1 once.

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

$out = Join-Path $proj "dist\NEU Helper"
if (-not (Test-Path (Join-Path $out "NEU Helper.exe"))) { throw "NEU Helper.exe was not produced" }

Say "copying the files that must sit next to the exe"
Copy-Item (Join-Path $proj "CLAUDE.example.md") $out
Copy-Item (Join-Path $proj "packaging\setup.ps1") $out
Copy-Item -Recurse (Join-Path $proj ".claude\skills") (Join-Path $out ".claude\skills")

$readme = @"
NEU Helper -- packaged build for Windows (no Python needed)

  1. Unzip this whole folder somewhere you will keep it
     (it writes data\ next to the exe: mail, chats, course files, settings).

  2. Run setup once, with a Canvas access token:

       powershell -ExecutionPolicy Bypass -File setup.ps1 -Token "14523~...."

     Get a token at: Canvas -> Account -> Settings -> New Access Token.

  3. Double-click "NEU Helper" on the Desktop.

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
    https://claude.com/claude-code
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
