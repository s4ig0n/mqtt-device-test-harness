<#
.SYNOPSIS
    One-step Windows setup: create the venv, install requirements, optionally
    start Mosquitto in Docker, then run pytest.

.DESCRIPTION
    Safe to run repeatedly. The venv is created only if missing, and pip skips
    packages that are already installed.

    Without -Mosquitto the suite uses its embedded amqtt broker, so Docker is
    not needed.

.PARAMETER Mosquitto
    Start (or reuse) an eclipse-mosquitto:2 container on port 1883 and run the
    tests against it instead of the embedded broker. Requires Docker.

.PARAMETER SkipTests
    Set everything up but don't run pytest.

.PARAMETER Python
    Python launcher to create the venv with. Defaults to "py -3" if the Windows
    launcher is installed, otherwise "python". Needs Python 3.11 or newer.

.PARAMETER PytestArgs
    Extra arguments for pytest. Put them after -- so PowerShell doesn't try
    to match them to this script's own parameters (-m would match -Mosquitto).

.EXAMPLE
    .\setup.ps1

.EXAMPLE
    .\setup.ps1 -Mosquitto

.EXAMPLE
    .\setup.ps1 -- -m smoke -v
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$Mosquitto,
    [switch]$SkipTests,
    [string]$Python,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$VenvDir = Join-Path $PSScriptRoot '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$ContainerName = 'mqtt-harness-mosquitto'
$MosquittoPort = 1883

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Checked {
    # Runs a native command and stops the script if it exits non-zero.
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "'$Exe $($Arguments -join ' ')' failed with exit code $LASTEXITCODE"
    }
}

# --- 1. Virtual environment -------------------------------------------------

if (-not (Test-Path -LiteralPath $VenvPython)) {
    if (-not $Python) {
        if (Get-Command py -ErrorAction SilentlyContinue) { $Python = 'py -3' }
        elseif (Get-Command python -ErrorAction SilentlyContinue) { $Python = 'python' }
        else { throw 'Python not found. Install Python 3.11+ from python.org and re-run.' }
    }
    $pyExe, $pyArgs = $Python -split '\s+'
    $pyArgs = @($pyArgs | Where-Object { $_ })

    # No quotes inside the Python code: Windows PowerShell 5.1 strips them when calling native exes.
    $version = & $pyExe @pyArgs -c 'import sys; print(*sys.version_info[:2], sep=chr(46))'
    if ($LASTEXITCODE -ne 0 -or [version]$version -lt [version]'3.11') {
        throw "Python 3.11+ is required, '$Python' is $version."
    }

    Write-Step "Creating virtual environment in .venv (Python $version)"
    Invoke-Checked $pyExe ($pyArgs + @('-m', 'venv', $VenvDir))
}
else {
    Write-Step 'Reusing existing .venv'
}

# --- 2. Requirements --------------------------------------------------------

Write-Step 'Installing requirements'
Invoke-Checked $VenvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', '--upgrade', 'pip')
Invoke-Checked $VenvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', '-r', 'requirements.txt')

# --- 3. Mosquitto (optional) ------------------------------------------------

if ($Mosquitto) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker not found. Install Docker Desktop, or run without -Mosquitto to use the embedded broker.'
    }
    # Windows PowerShell 5.1 turns native stderr into a terminating error under 'Stop'; relax it for this probe.
    $ErrorActionPreference = 'Continue'
    & docker info *> $null
    $dockerUp = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = 'Stop'
    if (-not $dockerUp) { throw 'Docker is installed but not running. Start Docker Desktop and re-run.' }

    $state = & docker ps -a --filter "name=^$ContainerName$" --format '{{.State}}'
    if ($state -eq 'running') {
        Write-Step "Mosquitto container '$ContainerName' already running"
    }
    elseif ($state) {
        Write-Step "Starting existing Mosquitto container '$ContainerName'"
        Invoke-Checked docker @('start', $ContainerName) | Out-Null
    }
    else {
        Write-Step "Starting Mosquitto container '$ContainerName' on port $MosquittoPort"
        Invoke-Checked docker @('run', '-d', '--name', $ContainerName, '-p', "${MosquittoPort}:1883",
            'eclipse-mosquitto:2', 'mosquitto', '-c', '/mosquitto-no-auth.conf') | Out-Null
    }

    # Wait until the broker accepts TCP connections.
    $deadline = (Get-Date).AddSeconds(20)
    while ($true) {
        $client = New-Object System.Net.Sockets.TcpClient
        try { $client.Connect('127.0.0.1', $MosquittoPort); break }
        catch {
            if ((Get-Date) -gt $deadline) { throw "Mosquitto did not open port $MosquittoPort within 20 s." }
            Start-Sleep -Milliseconds 500
        }
        finally { $client.Dispose() }
    }
}

# --- 4. Tests ---------------------------------------------------------------

if ($SkipTests) {
    Write-Step 'Setup done. Activate the venv with: .\.venv\Scripts\Activate.ps1'
    exit 0
}

$saved = @{ MQTT_HOST = $env:MQTT_HOST; MQTT_PORT = $env:MQTT_PORT }
try {
    if ($Mosquitto) {
        $env:MQTT_HOST = '127.0.0.1'
        $env:MQTT_PORT = "$MosquittoPort"
        Write-Step 'Running pytest against Mosquitto'
    }
    else {
        Write-Step 'Running pytest (embedded broker)'
    }
    & $VenvPython -m pytest @PytestArgs
    $testExit = $LASTEXITCODE
}
finally {
    $env:MQTT_HOST = $saved.MQTT_HOST
    $env:MQTT_PORT = $saved.MQTT_PORT
}

if ($Mosquitto) {
    Write-Host "Mosquitto is still running. Stop it with: docker rm -f $ContainerName"
}
exit $testExit
