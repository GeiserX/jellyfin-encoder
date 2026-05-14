#Requires -Version 5.1
<#
.SYNOPSIS
    Sets up Speaches (Whisper transcription server) on Windows via Docker in WSL2.
.DESCRIPTION
    Installs WSL2 Ubuntu, Docker Engine, NVIDIA Container Toolkit, and deploys
    the Speaches container with GPU passthrough. Creates a scheduled task for
    auto-start on boot.
.NOTES
    Prerequisites:
    - Windows 11 Pro with NVIDIA driver installed on host
    - NVIDIA GPU with CUDA support
    - Run as Administrator
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "=== Speaches WSL2 Setup ===" -ForegroundColor Cyan

# 1. Install/update WSL2
Write-Host "`n[1/4] Installing WSL2 with Ubuntu 22.04..." -ForegroundColor Yellow
wsl --install -d Ubuntu-22.04 --no-launch
wsl --update
wsl --set-default-version 2

# 2. Run setup inside WSL2
Write-Host "`n[2/4] Configuring Docker + NVIDIA Container Toolkit inside WSL2..." -ForegroundColor Yellow

$wslScript = @'
#!/bin/bash
set -euo pipefail

echo ">>> Installing Docker Engine..."
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

echo ">>> Installing NVIDIA Container Toolkit..."
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo service docker restart

echo ">>> Creating Speaches deployment..."
sudo mkdir -p /opt/speaches

sudo tee /opt/speaches/docker-compose.yml > /dev/null <<'COMPOSE'
services:
  speaches:
    image: ghcr.io/speaches-ai/speaches:v0.9.0-cuda
    container_name: speaches
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - speaches-models:/home/ubuntu/.cache/huggingface/hub
    environment:
      - WHISPER__INFERENCE_DEVICE=cuda
      - WHISPER__COMPUTE_TYPE=float16
      - WHISPER__DEVICE_INDEX=0
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

volumes:
  speaches-models:
COMPOSE

echo ">>> Configuring wsl.conf for auto-start..."
sudo tee /etc/wsl.conf > /dev/null <<'WSLCONF'
[boot]
command = service docker start && cd /opt/speaches && docker compose up -d
WSLCONF

echo ">>> Pulling image and starting service..."
sudo service docker start
cd /opt/speaches
sudo docker compose pull
sudo docker compose up -d

echo ">>> WSL2 setup complete!"
'@

# Write the script to a temp file and execute inside WSL2
$tempScript = [System.IO.Path]::GetTempFileName() + ".sh"
$wslScript | Set-Content -Path $tempScript -Encoding UTF8 -NoNewline
$wslPath = wsl -d Ubuntu-22.04 -- wslpath -u ($tempScript -replace '\\', '/')
wsl -d Ubuntu-22.04 -- bash $wslPath
Remove-Item -Path $tempScript -Force

# 3. Create Windows Task Scheduler task for auto-start
Write-Host "`n[3/4] Creating scheduled task 'WSL-Speaches'..." -ForegroundColor Yellow

$taskAction = New-ScheduledTaskAction `
    -Execute "wsl.exe" `
    -Argument "-d Ubuntu-22.04 -- bash -c `"service docker start && cd /opt/speaches && docker compose up -d`""

$taskTrigger = New-ScheduledTaskTrigger -AtStartup
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$taskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

Register-ScheduledTask `
    -TaskName "WSL-Speaches" `
    -Action $taskAction `
    -Trigger $taskTrigger `
    -Settings $taskSettings `
    -Principal $taskPrincipal `
    -Force | Out-Null

Write-Host "  Task 'WSL-Speaches' registered (runs at system startup as SYSTEM)." -ForegroundColor Green

# 4. Verify GPU access
Write-Host "`n[4/4] Verifying GPU passthrough..." -ForegroundColor Yellow
wsl -d Ubuntu-22.04 -- docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi

Write-Host "`n=== Setup Complete ===" -ForegroundColor Cyan
Write-Host "Speaches is running at http://localhost:8000" -ForegroundColor Green
Write-Host "Test with: Invoke-RestMethod -Uri 'http://localhost:8000/v1/models'" -ForegroundColor Green
