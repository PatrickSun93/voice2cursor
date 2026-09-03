' Adds (or with /remove, deletes) a Startup shortcut so the SonicType tray app
' comes up at login. Per-user Startup only -- no admin rights, no registry.
'
' This is the VBScript twin of install-autostart.ps1, for machines where
' PowerShell is blocked by policy. cscript/wscript are not restricted.
'
'   cscript //nologo install-autostart.vbs           ' add
'   cscript //nologo install-autostart.vbs /remove   ' remove
'
' Double-clicking works too; results are shown in a message box instead.

Option Explicit

Dim shell, fso, projectRoot, vbsPath, startupDir, linkPath
Dim doRemove, i, arg, isConsole

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' True when launched via cscript (console); False under wscript (double-click).
isConsole = (LCase(fso.GetFileName(WScript.FullName)) = "cscript.exe")

doRemove = False
For i = 0 To WScript.Arguments.Count - 1
    arg = LCase(WScript.Arguments(i))
    If arg = "/remove" Or arg = "-remove" Or arg = "--remove" Then
        doRemove = True
    ElseIf arg = "/?" Or arg = "-h" Or arg = "--help" Then
        Report "Usage: cscript //nologo install-autostart.vbs [/remove]"
        WScript.Quit 0
    Else
        Fail "Unknown argument: " & WScript.Arguments(i)
    End If
Next

' Derived, not hardcoded: this script always sits in the project root.
projectRoot = fso.GetParentFolderName(WScript.ScriptFullName)
vbsPath = fso.BuildPath(projectRoot, "run-silent.vbs")
startupDir = shell.SpecialFolders("Startup")

If Len(startupDir) = 0 Then
    Fail "Could not resolve the Startup folder."
End If
linkPath = fso.BuildPath(startupDir, "SonicType.lnk")

If doRemove Then
    If fso.FileExists(linkPath) Then
        On Error Resume Next
        fso.DeleteFile linkPath, True
        If Err.Number <> 0 Then
            Fail "Could not delete " & linkPath & vbCrLf & Err.Description
        End If
        On Error GoTo 0
        Report "Removed autostart shortcut:" & vbCrLf & linkPath
    Else
        Report "Nothing to remove -- no shortcut at" & vbCrLf & linkPath
    End If
    WScript.Quit 0
End If

If Not fso.FileExists(vbsPath) Then
    Fail "run-silent.vbs not found at " & vbsPath
End If

Dim link
On Error Resume Next
Set link = shell.CreateShortcut(linkPath)
' Target wscript.exe explicitly rather than the .vbs: a machine where the .vbs
' association is remapped or disabled would otherwise silently fail.
link.TargetPath = shell.ExpandEnvironmentStrings("%WINDIR%") & "\System32\wscript.exe"
link.Arguments = """" & vbsPath & """"
link.WorkingDirectory = projectRoot
link.Description = "SonicType push-to-talk dictation (tray)"
link.WindowStyle = 7  ' minimized; the launcher itself is already hidden
link.Save
If Err.Number <> 0 Then
    Fail "Could not write the shortcut:" & vbCrLf & Err.Description
End If
On Error GoTo 0

Report "Created autostart shortcut:" & vbCrLf & _
       linkPath & vbCrLf & _
       "  -> wscript.exe """ & vbsPath & """" & vbCrLf & vbCrLf & _
       "SonicType will start hidden at next login." & vbCrLf & _
       "Undo with: cscript //nologo install-autostart.vbs /remove"

WScript.Quit 0

' ------------------------------------------------------------------ helpers

Sub Report(msg)
    If isConsole Then
        WScript.Echo msg
    Else
        MsgBox msg, 64, "SonicType"
    End If
End Sub

Sub Fail(msg)
    If isConsole Then
        WScript.Echo "AUTOSTART SETUP FAILED"
        WScript.Echo "  " & msg
    Else
        MsgBox "AUTOSTART SETUP FAILED" & vbCrLf & vbCrLf & msg, 16, "SonicType"
    End If
    WScript.Quit 1
End Sub
