<#
    Starts SonicType with the console attached so logs and tracebacks are
    visible. Use run-silent.vbs instead for a windowless tray-only launch.

    Any extra arguments are forwarded to `python -m sonictype`.

    Windows PowerShell 5.1 -- no &&, ||, ?:, or ??.
#>
# No [CmdletBinding()] on purpose: it turns unrecognised -flags into a binding
# error, and this script has to forward whatever the user typed to the app.
param(
    [string]$EnvName = 'sonictype'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = $PSScriptRoot
$AppArgs = $args

function Resolve-EnvPython {
    param([string]$Name)

    # Same probe order as setup.ps1: a bare `python` on this machine resolves to
    # a stale C:\Python34, so never fall back to PATH for the interpreter.
    $condaRoots = @(
        (Join-Path $env:LOCALAPPDATA 'miniconda3'),
        (Join-Path $env:LOCALAPPDATA 'Anaconda3'),
        (Join-Path $env:USERPROFILE 'miniconda3')
    )
    foreach ($root in $condaRoots) {
        $py = Join-Path $root ('envs\{0}\python.exe' -f $Name)
        if (Test-Path -LiteralPath $py) { return $py }
    }

    # Last resort: ask conda itself where the env lives.
    $cmd = Get-Command conda -ErrorAction SilentlyContinue
    if ($cmd) {
        $rows = & $cmd.Source env list
        foreach ($row in $rows) {
            $line = $row.Trim()
            if ($line.Length -eq 0) { continue }
            if ($line.StartsWith('#')) { continue }
            $fields = $line -split '\s+'
            if ($fields[0] -eq $Name) {
                $py = Join-Path $fields[-1] 'python.exe'
                if (Test-Path -LiteralPath $py) { return $py }
            }
        }
    }

    throw "Could not find the '$Name' conda env. Run .\setup.ps1 first."
}

try {
    $envPython = Resolve-EnvPython -Name $EnvName

    # cwd must be the project root for `python -m sonictype` to find the package.
    Push-Location $ProjectRoot
    try {
        if ($AppArgs -and $AppArgs.Count -gt 0) {
            & $envPython -m sonictype @AppArgs
        }
        else {
            & $envPython -m sonictype
        }
        $code = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    if ($code -ne 0) {
        Write-Host ''
        Write-Host ("SonicType exited with code {0}." -f $code) -ForegroundColor Yellow
    }
    exit $code
}
catch {
    Write-Host ''
    Write-Host 'LAUNCH FAILED' -ForegroundColor Red
    Write-Host ("  {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
