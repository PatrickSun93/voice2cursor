' Double-clickable launcher for setup.py, for machines where PowerShell is
' blocked by policy and setup.ps1 therefore cannot run.
'
' Its only job is the bootstrap problem: setup.py has to run BEFORE the
' voice2cursor env exists, so it needs conda's *base* interpreter -- and a bare
' `python` on this machine resolves to a stale C:\Python34. This finds the
' right one and hands off.
'
' Any arguments are forwarded to setup.py, e.g.:
'   cscript //nologo setup.vbs --verify-only

Option Explicit

Dim shell, fso, projectRoot, setupPy, bootstrap, cmd, i, argList, code

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Derived, not hardcoded: this script always sits in the project root.
' This script lives in scripts\windows; the project is two levels up.
projectRoot = fso.GetParentFolderName(fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName)))
setupPy = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "setup.py")

If Not fso.FileExists(setupPy) Then
    MsgBox "setup.py not found at" & vbCrLf & setupPy, 16, "voice2cursor"
    WScript.Quit 1
End If

bootstrap = FindBootstrapPython()
If Len(bootstrap) = 0 Then
    MsgBox "Could not find a conda base interpreter." & vbCrLf & vbCrLf & _
           "Install Miniconda for the current user, then re-run:" & vbCrLf & _
           "  winget install --id Anaconda.Miniconda3 --scope user", 16, "voice2cursor"
    WScript.Quit 1
End If

' Forward whatever the user passed through to setup.py.
argList = ""
For i = 0 To WScript.Arguments.Count - 1
    argList = argList & " """ & WScript.Arguments(i) & """"
Next

' cwd must be the project root so setup.py's relative paths and the verify
' step's `import voice2cursor.*` both resolve.
shell.CurrentDirectory = projectRoot

' python.exe is invoked directly rather than through cmd.exe: this machine bans
' PowerShell by policy and cmd may be restricted too, so the fewer shells in the
' chain the better. setup.py's --pause is what keeps the console readable after
' a solve that fails at minute three, instead of the window vanishing.
cmd = """" & bootstrap & """ """ & setupPy & """ --pause" & argList

' 1 = normal visible window, True = wait for it to exit.
code = shell.Run(cmd, 1, True)
WScript.Quit code

' ------------------------------------------------------------------ helpers

Function FindBootstrapPython()
    ' Same probe order as setup.py's resolve_conda, one level up: the base
    ' install's python.exe, never the voice2cursor env (it may not exist yet).
    Dim roots, r, candidate
    roots = Array( _
        shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\miniconda3", _
        shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Anaconda3", _
        shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\miniconda3")

    For Each r In roots
        candidate = r & "\python.exe"
        If fso.FileExists(candidate) Then
            FindBootstrapPython = candidate
            Exit Function
        End If
    Next

    FindBootstrapPython = ""
End Function
