@echo off
rem NEU Helper launcher. ASCII-only on purpose: cmd.exe decodes .cmd files
rem with the OEM codepage (GBK here), so any CJK text would come out garbled.
rem All Chinese output lives in brief.py and CLAUDE.md, which are read as UTF-8.

chcp 65001 >nul 2>&1
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

python brief.py
if errorlevel 1 (
  echo.
  echo   [!] Canvas fetch failed -- see the message above.
  echo   [!] Ask in the chat below: "Canvas connection is broken, help me fix it"
)

echo.
echo   ---------------------------------------------------------------
echo    Handing off to Claude. Ctrl+C to skip, /exit to quit.
echo   ---------------------------------------------------------------
echo.

claude "boot-briefing"

echo.
echo   Session ended. This window stays open so you can scroll back.
endlocal
