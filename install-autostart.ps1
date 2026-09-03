<#
    Adds (or with -Remove, deletes) a Startup shortcut so the SonicType tray app
    comes up at login. Per-user Startup only -- no admin rights, no registry.

    Safe to re-run: the shortcut is simply rewritten.

    Windows PowerShell 5.1 -- no &&, ||, ?:, or ??.
#>
[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = $PSScriptRoot
$VbsPath = Join-Path $ProjectRoot 'run-silent.vbs'
$StartupDir = [Environment]::GetFolderPath('Startup')
$LinkPath = Join-Path $StartupDir 'SonicType.lnk'

try {
    if ($Remove) {
        if (Test-Path -LiteralPath $LinkPath) {
            Remove-Item -LiteralPath $LinkPath -Force
            Write-Host "Removed autostart shortcut: $LinkPath" -ForegroundColor Green
        }
        else {
            Write-Host "Nothing to remove -- no shortcut at $LinkPath"
        }
        exit 0
    }

    if (-not (Test-Path -LiteralPath $VbsPath)) {
        throw "run-silent.vbs not found at $VbsPath"
    }
    if (-not (Test-Path -LiteralPath $StartupDir)) {
        throw "Startup folder not found at $StartupDir"
    }

    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($LinkPath)

    # Target wscript.exe explicitly rather than the .vbs: a machine where the
    # .vbs association is remapped or disabled would otherwise silently fail.
    $link.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
    $link.Arguments = '"{0}"' -f $VbsPath
    $link.WorkingDirectory = $ProjectRoot
    $link.Description = 'SonicType push-to-talk dictation (tray)'
    $link.WindowStyle = 7  # minimized; the launcher itself is already hidden
    $link.Save()

    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)

    Write-Host "Created autostart shortcut: $LinkPath" -ForegroundColor Green
    Write-Host "  -> wscript.exe `"$VbsPath`""
    Write-Host 'SonicType will start hidden at next login. Undo with: .\install-autostart.ps1 -Remove'
}
catch {
    Write-Host ''
    Write-Host 'AUTOSTART SETUP FAILED' -ForegroundColor Red
    Write-Host ("  {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
