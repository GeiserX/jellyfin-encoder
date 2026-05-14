# windows-start-encoder.ps1
# Launcher script for Task Scheduler. Reads .env file and runs monitor.py.
# Usage: powershell -ExecutionPolicy Bypass -File C:\jellyfin-encoder\windows-start-encoder.ps1 -EnvFile C:\jellyfin-encoder\encoder.env

param(
    [Parameter(Mandatory=$true)]
    [string]$EnvFile
)

if (-not (Test-Path $EnvFile)) {
    Write-Error "Environment file not found: $EnvFile"
    exit 1
}

Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith('#')) {
        $parts = $line -split '=', 2
        if ($parts.Count -eq 2) {
            [System.Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), 'Process')
        }
    }
}

$python = "python"
$script = "C:\jellyfin-encoder\app\monitor.py"

if (-not (Test-Path $script)) {
    Write-Error "monitor.py not found at: $script"
    exit 1
}

& $python $script
