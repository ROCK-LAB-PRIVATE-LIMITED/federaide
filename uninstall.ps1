# uninstaller.ps1
# ==============================================================================
#            FEDERaiDE.AI Native Windows PowerShell Uninstaller Script
# ==============================================================================
$ErrorActionPreference = "Continue"

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "          FEDERaiDE.AI Universal uv-Based Uninstaller                   " -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan

# Temporarily append the default user local bin to the PATH for this session
$userBinPath = Join-Path $env:USERPROFILE ".local\bin"
if ($env:PATH -notlike "*$userBinPath*") {
    $env:PATH = "$userBinPath;" + $env:PATH
}

# 1. Attempt uninstallation via uv tool
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Host "[*] Removing FEDERaiDE executable and virtual environments via uv..." -ForegroundColor Yellow
    try {
        uv tool uninstall federaide
    } catch {
        Write-Host "[!] 'uv tool uninstall' encountered an issue. Proceeding to manual cleanup..." -ForegroundColor Yellow
    }
} else {
    Write-Host "[!] 'uv' command not detected on your environment PATH." -ForegroundColor Yellow
    Write-Host "[*] Performing direct filesystem purge of the isolated tool environment..." -ForegroundColor Yellow
    
    # 2. Filesystem cleanup fallback
    $uvToolsLocal = Join-Path $env:LOCALAPPDATA "uv\tools\federaide"
    $uvToolsRoaming = Join-Path $env:APPDATA "uv\tools\federaide"
    $uvBinPath = Join-Path $env:USERPROFILE ".local\bin\federaide.exe"

    if (Test-Path $uvToolsLocal) {
        Write-Host "    [-] Removing local application workspace tool files..." -ForegroundColor Yellow
        Remove-Item -Recurse -Force $uvToolsLocal -ErrorAction SilentlyContinue
    }
    if (Test-Path $uvToolsRoaming) {
        Write-Host "    [-] Removing roaming application workspace tool files..." -ForegroundColor Yellow
        Remove-Item -Recurse -Force $uvToolsRoaming -ErrorAction SilentlyContinue
    }
    if (Test-Path $uvBinPath) {
        Write-Host "    [-] Removing executable wrapper..." -ForegroundColor Yellow
        Remove-Item -Force $uvBinPath -ErrorAction SilentlyContinue
    }
}

Write-Host "======================================================================" -ForegroundColor Green
Write-Host " 🎉 FEDERaiDE has been successfully uninstalled." -ForegroundColor Green
Write-Host " Note: Your local configuration databases, models, and workspaces " -ForegroundColor Green
Write-Host " have been preserved." -ForegroundColor Green
Write-Host "======================================================================" -ForegroundColor Green