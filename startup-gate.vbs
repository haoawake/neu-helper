' NEU Helper startup gate.
'
' Runs hidden at logon, decides whether to start the app today, and if so
' launches it. Doing the check here (rather than inside the app) means nothing
' at all happens on boots that are already handled.
'
' ---------------------------------------------------------------------------
' Launch flags matter here, and the obvious choices are both wrong:
'
'   style 0 (SW_HIDE) -- DO NOT USE. The flag lands in the child's STARTUPINFO,
'   and WinForms applies it to the first form the process shows. The result is
'   that hiding the console also hides the app window: verified by enumerating
'   the process's top-level windows, the WebView2 host came back visible=False
'   and the app looked like it had silently failed to start.
'
'   python.exe -- would need style 0 to keep its console off screen, which
'   runs straight into the problem above.
'
' So: pythonw.exe (no console exists at all) with style 1 (normal). Verified
' visible=True, MainWindowHandle non-zero, and no new conhost process.
' ---------------------------------------------------------------------------
Option Explicit

Dim fso, sh, proj, marker, todayStr, lastStr, f, py, oncePerDay

' Set to False if you want the app launched on EVERY boot instead of once a day.
' (The daily briefing is separately capped at one per calendar day, so extra
' launches will not produce extra briefings either way.)
oncePerDay = True

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

proj   = fso.GetParentFolderName(WScript.ScriptFullName)
marker = proj & "\data\last-launch.txt"
todayStr = Year(Date) & "-" & Right("0" & Month(Date), 2) & "-" & Right("0" & Day(Date), 2)

lastStr = ""
If fso.FileExists(marker) Then
  Set f = fso.OpenTextFile(marker, 1)
  If Not f.AtEndOfStream Then lastStr = Trim(f.ReadLine())
  f.Close
End If

If oncePerDay And lastStr = todayStr Then WScript.Quit 0

If Not fso.FolderExists(proj & "\data") Then fso.CreateFolder proj & "\data"
Set f = fso.CreateTextFile(marker, True)
f.WriteLine todayStr
f.Close

' install.ps1 records the resolved interpreter here, because PATH at logon is
' not always the same PATH an interactive shell sees.
py = ""
If fso.FileExists(proj & "\data\pythonw-path.txt") Then
  Set f = fso.OpenTextFile(proj & "\data\pythonw-path.txt", 1)
  If Not f.AtEndOfStream Then py = Trim(f.ReadLine())
  f.Close
End If
If py = "" Or Not fso.FileExists(py) Then py = "pythonw.exe"

' 1 = normal window. See the header: 0 would hide the app window too.
sh.Run Chr(34) & py & Chr(34) & " " & Chr(34) & proj & "\app.py" & Chr(34), 1, False
