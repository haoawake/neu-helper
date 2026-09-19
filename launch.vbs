' NEU Helper launcher -- double-click this to open the app.

'

' Differences from startup-gate.vbs (the logon one):

'   - no once-per-day gate: this always opens the app

'   - that's it; both end up running pythonw app.py

'

' Why a .vbs at all: wscript.exe starts a process without ever creating a

' console window, so there is no black flash. A .cmd/.bat cannot do that.

' ASCII only on purpose -- see the comment in start-canvas-helper.cmd.

'

' Running it twice is harmless: app.py holds a named mutex and the second

' instance just raises the first one's window and exits.

Option Explicit



Dim fso, sh, proj, py, f

Set fso = CreateObject("Scripting.FileSystemObject")

Set sh = CreateObject("WScript.Shell")

proj = fso.GetParentFolderName(WScript.ScriptFullName)



' install.ps1 records the resolved interpreter; PATH at logon is not always

' the PATH an interactive shell sees.

py = ""

If fso.FileExists(proj & "\data\pythonw-path.txt") Then

  Set f = fso.OpenTextFile(proj & "\data\pythonw-path.txt", 1)

  If Not f.AtEndOfStream Then py = Trim(f.ReadLine())

  f.Close

End If

If py = "" Or Not fso.FileExists(py) Then py = "pythonw.exe"



' 1 = normal window. 0 would hide the app window too -- see startup-gate.vbs.

sh.CurrentDirectory = proj

sh.Run Chr(34) & py & Chr(34) & " " & Chr(34) & proj & "\app.py" & Chr(34), 1, False

