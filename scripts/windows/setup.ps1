<#
    Creates or updates the `voice2cursor` conda env, then proves the three things
    that actually break on a fresh machine: the interpreter, CUDA visibility
    through the pip-installed NVIDIA DLLs, and a usable input device.

    Safe to re-run. Windows PowerShell 5.1 -- no &&, ||, ?:, or ??.
#>
[CmdletBinding()]
param(
    [string]$EnvName = 'voice2cursor'
)

$ErrorActionPreference = 'Stop'

# This script lives in scripts\windows; the project is two levels up.
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$EnvFile = Join-Path $ProjectRoot 'environment.yml'

function Resolve-Conda {
    # The user's PATH carries a stale C:\Python34 and no miniconda, so probe the
    # known install locations before trusting PATH at all.
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'miniconda3\Scripts\conda.exe'),
        (Join-Path $env:LOCALAPPDATA 'Anaconda3\Scripts\conda.exe'),
        (Join-Path $env:USERPROFILE 'miniconda3\Scripts\conda.exe')
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    $cmd = Get-Command conda -ErrorAction SilentlyContinue
    if ($cmd) {
        if ($cmd.Source) { return $cmd.Source }
        return $cmd.Name
    }
    $checked = $candidates -join "`n  "
    throw @"
conda was not found. Install Miniconda for the current user, then re-run this script:

    winget install --id Anaconda.Miniconda3 --scope user

Checked:
  $checked
  PATH (Get-Command conda)
"@
}

function Test-CondaEnv {
    param([string]$Conda, [string]$Name)
    # `conda env list` prints "<name>  [*]  <path>"; comment lines start with #.
    $rows = & $Conda env list
    if (-not $rows) { throw 'conda env list returned nothing -- is this conda install healthy?' }
    foreach ($row in $rows) {
        $line = $row.Trim()
        if ($line.Length -eq 0) { continue }
        if ($line.StartsWith('#')) { continue }
        $fields = $line -split '\s+'
        if ($fields[0] -eq $Name) { return $true }
    }
    return $false
}

function Get-CondaEnvPath {
    param([string]$Conda, [string]$Name)
    $rows = & $Conda env list
    foreach ($row in $rows) {
        $line = $row.Trim()
        if ($line.Length -eq 0) { continue }
        if ($line.StartsWith('#')) { continue }
        $fields = $line -split '\s+'
        if ($fields[0] -eq $Name) { return $fields[-1] }
    }
    return $null
}

# Runs inside the target env and reports the facts as one JSON line, so this
# script never has to guess whether the CUDA DLL shim worked.
$VerifyScript = @'
import json, sys

out = {"python": sys.version.split()[0], "prefix": sys.prefix,
       "cuda": False, "inputs": 0, "notes": []}
try:
    from voice2cursor.cuda_setup import cuda_available
    out["cuda"] = bool(cuda_available())
except Exception as exc:
    out["notes"].append("cuda: %s: %s" % (type(exc).__name__, exc))
try:
    from voice2cursor.audio import list_input_devices
    out["inputs"] = len(list_input_devices())
except Exception as exc:
    out["notes"].append("audio: %s: %s" % (type(exc).__name__, exc))
print("VOICE2CURSOR_VERIFY " + json.dumps(out))
'@

try {
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        throw "environment.yml not found at $EnvFile"
    }

    $conda = Resolve-Conda
    Write-Host "conda:   $conda"
    Write-Host "project: $ProjectRoot"
    Write-Host ''

    if (Test-CondaEnv -Conda $conda -Name $EnvName) {
        Write-Host "Env '$EnvName' exists -- updating from environment.yml (--prune)..." -ForegroundColor Cyan
        & $conda env update --name $EnvName --file $EnvFile --prune
    }
    else {
        Write-Host "Env '$EnvName' not found -- creating from environment.yml..." -ForegroundColor Cyan
        & $conda env create --name $EnvName --file $EnvFile
    }
    if ($LASTEXITCODE -ne 0) { throw "conda env create/update failed (exit $LASTEXITCODE)." }

    $envPath = Get-CondaEnvPath -Conda $conda -Name $EnvName
    if (-not $envPath) { throw "Env '$EnvName' still not listed by conda after create/update." }
    $envPython = Join-Path $envPath 'python.exe'
    if (-not (Test-Path -LiteralPath $envPython)) { throw "No python.exe at $envPython" }

    Write-Host ''
    Write-Host 'Verifying...' -ForegroundColor Cyan

    # cwd must be the project root so `import voice2cursor.*` resolves.
    Push-Location $ProjectRoot
    try {
        $raw = $VerifyScript | & $envPython '-'
    }
    finally {
        Pop-Location
    }

    $jsonLine = $raw | Where-Object { $_ -like 'VOICE2CURSOR_VERIFY *' } | Select-Object -First 1
    if (-not $jsonLine) {
        Write-Host ($raw -join [Environment]::NewLine)
        throw 'Verification script produced no result line.'
    }
    $info = ($jsonLine -replace '^VOICE2CURSOR_VERIFY ', '') | ConvertFrom-Json

    $warnings = @()

    Write-Host ''
    Write-Host '--- summary -------------------------------------------------'
    Write-Host ("PASS  python {0}" -f $info.python)
    Write-Host ("      prefix {0}" -f $info.prefix)

    if ($info.cuda) {
        Write-Host 'PASS  ctranslate2 sees a CUDA device'
    }
    else {
        Write-Host 'WARN  no CUDA device visible to ctranslate2 -- transcription will run on CPU' -ForegroundColor Yellow
        $warnings += 'cuda'
    }

    if ($info.inputs -gt 0) {
        Write-Host ("PASS  {0} audio input device(s)" -f $info.inputs)
    }
    else {
        Write-Host 'WARN  no audio input devices found -- check the mic and Windows privacy settings' -ForegroundColor Yellow
        $warnings += 'audio'
    }

    foreach ($note in $info.notes) {
        Write-Host ("      note: {0}" -f $note) -ForegroundColor DarkYellow
    }

    # Ollama is the user's own install; only report on it.
    $ollama = $null
    try {
        $ollama = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 -UseBasicParsing
    }
    catch {
        $ollama = $null
    }
    if ($ollama) {
        $names = @()
        if ($ollama.PSObject.Properties.Name -contains 'models') {
            $names = @($ollama.models | ForEach-Object { $_.name } | Where-Object { $_ })
        }
        if ($names.Count -gt 0) {
            Write-Host ("PASS  Ollama is up with {0} model(s): {1}" -f $names.Count, ($names -join ', '))
        }
        else {
            Write-Host 'WARN  Ollama is up but has no models pulled (try: ollama pull llama3.2)' -ForegroundColor Yellow
            $warnings += 'ollama-models'
        }
    }
    else {
        Write-Host 'WARN  Ollama not answering on 127.0.0.1:11434 -- start it before using cleanup features' -ForegroundColor Yellow
        $warnings += 'ollama'
    }

    Write-Host '-------------------------------------------------------------'
    if ($warnings.Count -eq 0) {
        Write-Host 'Setup complete. Run .\scripts\windows\run.ps1 to start voice2cursor.' -ForegroundColor Green
    }
    else {
        Write-Host ("Setup complete with {0} warning(s): {1}" -f $warnings.Count, ($warnings -join ', ')) -ForegroundColor Yellow
        Write-Host 'The app will still start. Run .\run.ps1 to try it.'
    }
}
catch {
    Write-Host ''
    Write-Host 'SETUP FAILED' -ForegroundColor Red
    Write-Host ("  {0}" -f $_.Exception.Message) -ForegroundColor Red
    if ($_.InvocationInfo -and $_.InvocationInfo.ScriptLineNumber) {
        Write-Host ("  at line {0}" -f $_.InvocationInfo.ScriptLineNumber) -ForegroundColor DarkGray
    }
    Write-Host ''
    Write-Host 'Nothing was left half-installed that a re-run cannot fix -- fix the cause above and run setup.ps1 again.'
    exit 1
}
