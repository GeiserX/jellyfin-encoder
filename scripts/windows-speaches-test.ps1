#Requires -Version 5.1
<#
.SYNOPSIS
    Tests that the Speaches API is running and responding on Windows.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$uri = "http://localhost:8000/v1/models"

Write-Host "Testing Speaches API at $uri ..." -ForegroundColor Cyan

try {
    $response = Invoke-RestMethod -Uri $uri -Method Get -TimeoutSec 10
    Write-Host "`nSpeaches is running. Available models:" -ForegroundColor Green
    if ($response.data -and $response.data.Count -gt 0) {
        $response.data | ForEach-Object {
            Write-Host "  - $($_.id)" -ForegroundColor White
        }
    } else {
        Write-Host "  No models loaded yet (first request may trigger download)." -ForegroundColor Yellow
    }
} catch {
    Write-Host "`nFAILED: Speaches is not responding." -ForegroundColor Red
    Write-Host "Error: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "`nTroubleshooting:" -ForegroundColor Yellow
    Write-Host "  1. Check WSL2 is running: wsl -l --running"
    Write-Host "  2. Check container: wsl -d Ubuntu-22.04 -- docker ps"
    Write-Host "  3. Check logs: wsl -d Ubuntu-22.04 -- docker logs speaches"
    exit 1
}
