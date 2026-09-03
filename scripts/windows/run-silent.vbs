' Starts voice2cursor with no console window -- the app lives in the tray, so a
' flashing cmd window on login is pure noise. Used by install-autostart.ps1.
'
' pythonw.exe (not python.exe) is what keeps the process windowless.

Option Explicit

Dim shell, fso, projectRoot, pythonw

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Derived, not hardcoded: this script always sits in the project root.
' This script lives in scripts\windows; the project is two levels up.
projectRoot = fso.GetParentFolderName(fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName)))

' %LOCALAPPDATA% keeps this user-agnostic; adjust if miniconda lives elsewhere.
pythonw = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & _
          "\miniconda3\envs\voice2cursor\pythonw.exe"

If Not fso.FileExists(pythonw) Then
    MsgBox "voice2cursor: interpreter not found at" & vbCrLf & pythonw & vbCrLf & vbCrLf & _
           "Run scripts\windows\setup.ps1 in " & projectRoot & " first.", 16, "voice2cursor"
    WScript.Quit 1
End If

' CurrentDirectory must be the project root so "python -m voice2cursor" resolves.
shell.CurrentDirectory = projectRoot

' 0 = hidden window, False = do not wait for the app to exit.
shell.Run """" & pythonw & """ -m voice2cursor", 0, False
