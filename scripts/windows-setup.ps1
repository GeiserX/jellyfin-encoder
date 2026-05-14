# windows-setup.ps1
# Setup script for running jellyfin-encoder natively on Windows 11 with NVIDIA GPU.
# Run as Administrator: powershell -ExecutionPolicy Bypass -File windows-setup.ps1

#Requires -RunAsAdministrator

param(
    [string]$SmbServer = "YOUR_SERVER_IP",
    [string]$SmbShare = "ShareMedia",
    [string]$DriveLetter = "S",
    [string]$WorkDir = "C:\jellyfin-encoder"
)

$ErrorActionPreference = "Stop"
$AppDir = "$WorkDir\app"
$ScriptsDir = "$WorkDir\scripts"

# --- Step 1: Install prerequisites ---
Write-Host "`n[1/8] Installing prerequisites..." -ForegroundColor Cyan

$packages = @(
    @{ Id = "Python.Python.3.12"; Name = "Python 3.12" },
    @{ Id = "Gyan.FFmpeg"; Name = "FFmpeg (NVENC)" }
)

foreach ($pkg in $packages) {
    $installed = winget list --id $pkg.Id 2>$null | Select-String $pkg.Id
    if ($installed) {
        Write-Host "  $($pkg.Name) already installed, skipping." -ForegroundColor Green
    } else {
        Write-Host "  Installing $($pkg.Name)..."
        winget install --id $pkg.Id --accept-source-agreements --accept-package-agreements
    }
}

# --- Step 2: Create working directory ---
Write-Host "`n[2/8] Creating working directory..." -ForegroundColor Cyan

if (-not (Test-Path $WorkDir)) {
    New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null
    Write-Host "  Created $WorkDir"
} else {
    Write-Host "  $WorkDir already exists, skipping."
}

# --- Step 3: Create environment config files ---
Write-Host "`n[3/8] Creating environment config files..." -ForegroundColor Cyan

$envPeliculas = @"
ENABLE_HW_ACCEL=true
HW_ENCODING_TYPE=nvidia
ENCODING_QUALITY=LOW
ENCODING_CODEC=hevc
MAX_HW_WORKERS=2
SOURCE_FOLDER=$${DriveLetter}:\Peliculas
DEST_FOLDER=$WorkDir\output\Peliculas
SYMLINK_MANIFEST_TARGET=/media-720/Peliculas
SYMLINK_VERSION_SUFFIX= - 720p
TZ=Europe/Madrid
"@

$envSeries = @"
ENABLE_HW_ACCEL=true
HW_ENCODING_TYPE=nvidia
ENCODING_QUALITY=LOW
ENCODING_CODEC=hevc
MAX_HW_WORKERS=2
SOURCE_FOLDER=$${DriveLetter}:\Series
DEST_FOLDER=$WorkDir\output\Series
SYMLINK_MANIFEST_TARGET=/media-720/Series
SYMLINK_VERSION_SUFFIX= - 720p
TZ=Europe/Madrid
"@

Set-Content -Path "$WorkDir\encoder.env" -Value $envPeliculas -Encoding UTF8
Write-Host "  Written $WorkDir\encoder.env"

Set-Content -Path "$WorkDir\encoder-series.env" -Value $envSeries -Encoding UTF8
Write-Host "  Written $WorkDir\encoder-series.env"

# --- Step 4: Map SMB drive ---
Write-Host "`n[4/8] Mapping SMB share..." -ForegroundColor Cyan

$smbPath = "\\$SmbServer\$SmbShare"
$driveMapping = "${DriveLetter}:"
$existing = Get-SmbMapping 2>$null | Where-Object { $_.LocalPath -eq $driveMapping }

if ($existing) {
    Write-Host "  Drive $driveMapping already mapped to $($existing.RemotePath), skipping."
} else {
    Write-Host "  Enter credentials for $smbPath"
    $cred = Get-Credential -Message "SMB credentials for $smbPath"
    New-SmbMapping -LocalPath $driveMapping -RemotePath $smbPath -UserName $cred.UserName -Password $cred.GetNetworkCredential().Password -Persistent $true
    Write-Host "  Mapped $smbPath to $driveMapping (persistent)."
}

# --- Step 5: Windows Defender exclusions ---
Write-Host "`n[5/8] Adding Windows Defender exclusions..." -ForegroundColor Cyan

$pathExclusions = @(
    $WorkDir,
    "\\$SmbServer\$SmbShare"
)

$processExclusions = @(
    "ffmpeg.exe",
    "python.exe"
)

foreach ($path in $pathExclusions) {
    $current = (Get-MpPreference).ExclusionPath
    if ($current -and $current -contains $path) {
        Write-Host "  Path exclusion already exists: $path"
    } else {
        Add-MpPreference -ExclusionPath $path
        Write-Host "  Added path exclusion: $path"
    }
}

foreach ($proc in $processExclusions) {
    $current = (Get-MpPreference).ExclusionProcess
    if ($current -and $current -contains $proc) {
        Write-Host "  Process exclusion already exists: $proc"
    } else {
        Add-MpPreference -ExclusionProcess $proc
        Write-Host "  Added process exclusion: $proc"
    }
}

# --- Step 6: Create Task Scheduler tasks ---
Write-Host "`n[6/8] Creating Task Scheduler tasks..." -ForegroundColor Cyan

$launcherScript = "$WorkDir\scripts\windows-start-encoder.ps1"

$tasks = @(
    @{
        Name    = "JellyfinEncoder-Peliculas"
        EnvFile = "$WorkDir\encoder.env"
    },
    @{
        Name    = "JellyfinEncoder-Series"
        EnvFile = "$WorkDir\encoder-series.env"
    }
)

foreach ($task in $tasks) {
    $existingTask = Get-ScheduledTask -TaskName $task.Name -ErrorAction SilentlyContinue
    if ($existingTask) {
        Write-Host "  Task '$($task.Name)' already exists, removing to recreate..."
        Unregister-ScheduledTask -TaskName $task.Name -Confirm:$false
    }

    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-ExecutionPolicy Bypass -NonInteractive -File `"$launcherScript`" -EnvFile `"$($task.EnvFile)`"" `
        -WorkingDirectory $WorkDir

    $trigger = New-ScheduledTaskTrigger -AtStartup

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Duration 0)

    $principal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Highest

    Register-ScheduledTask `
        -TaskName $task.Name `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Jellyfin encoder monitor ($($task.Name))" | Out-Null

    Write-Host "  Created task: $($task.Name)"
}

# --- Step 7: Install Python dependencies ---
Write-Host "`n[7/8] Installing Python dependencies..." -ForegroundColor Cyan

& python -m pip install --upgrade pip 2>$null
& python -m pip install watchdog
Write-Host "  Installed watchdog."

# --- Step 8: Disable sleep/hibernate ---
Write-Host "`n[8/8] Disabling sleep and hibernate..." -ForegroundColor Cyan

powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
Write-Host "  Standby and hibernate disabled on AC power."

# --- Copy app files reminder ---
Write-Host "`n" -NoNewline
Write-Host "============================================" -ForegroundColor Yellow
Write-Host " SETUP COMPLETE" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Yellow
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Copy the 'app' folder to $AppDir"
Write-Host "  2. Copy 'scripts\windows-start-encoder.ps1' to $ScriptsDir\"
Write-Host "  3. Reboot to start the encoder tasks automatically"
Write-Host "  4. Or start manually:"
Write-Host "     Start-ScheduledTask -TaskName 'JellyfinEncoder-Peliculas'"
Write-Host "     Start-ScheduledTask -TaskName 'JellyfinEncoder-Series'"
Write-Host ""
