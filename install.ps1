# install.ps1
# ==============================================================================
#            FEDERaiDE Native Windows PowerShell Installer Script
# ==============================================================================
$ErrorActionPreference = "Stop"

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "          FEDERaiDE Universal uv-Based Installer Bootstrapper        " -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan

# 1. Ensure uv is installed
Write-Host "[*] Checking for uv installation..." -ForegroundColor Yellow
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    
    # Try using winget first (the cleanest and safest way on Windows)
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "[*] uv not detected. Installing via winget..." -ForegroundColor Yellow
        
        # Install uv silently while accepting agreements
        winget install --id astral-sh.uv --silent --accept-source-agreements --accept-package-agreements
        
        # Refresh PATH environment variables so the active session can find the new winget installation path
        $env:PATH = [System.Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'Machine')
    } 
    # Fallback only if winget is not available (e.g. on minimal Windows Server builds)
    else {
        Write-Host "[!] winget not found. Falling back to direct binary download..." -ForegroundColor Yellow
        
        $arch = "x86_64"
        if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") {
            $arch = "aarch64"
        } elseif (-not [System.Environment]::Is64BitOperatingSystem) {
            $arch = "i686"
        }

        $localBinDir = Join-Path $HOME ".local\bin"
        if (-not (Test-Path $localBinDir)) {
            New-Item -ItemType Directory -Force -Path $localBinDir | Out-Null
        }

        $zipPath = Join-Path $env:TEMP "uv.zip"
        $extractPath = Join-Path $env:TEMP "uv_extracted"
        if (Test-Path $extractPath) { Remove-Item -Recurse -Force $extractPath }

        $downloadUrl = "https://github.com/astral-sh/uv/releases/latest/download/uv-$arch-pc-windows-msvc.zip"
        Invoke-RestMethod -Uri $downloadUrl -OutFile $zipPath
        Expand-Archive -Path $zipPath -DestinationPath $extractPath
        Get-ChildItem -Path $extractPath -Filter "*.exe" -Recurse | Copy-Item -Destination $localBinDir -Force

        Remove-Item -Force $zipPath -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $extractPath -ErrorAction SilentlyContinue

        $env:PATH = "$localBinDir;" + $env:PATH
    }

    # Verify installation succeeded
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "Could not locate 'uv' even after installation. Please restart your terminal and try again."
    }
} else {
    Write-Host "[*] uv is already available on your PATH." -ForegroundColor Green
}

# 2. Ensure the local bin folder where uv places tools is permanently in the User's PATH
$localBin = Join-Path $HOME ".local\bin"
if (-not (Test-Path $localBin)) {
    New-Item -ItemType Directory -Force -Path $localBin | Out-Null
}

$userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
if ($null -eq $userPath) { $userPath = "" }
$pathList = $userPath -split ';' | Where-Object { $_ -and $_.Trim() -ne "" }

if ($pathList -notcontains $localBin) {
    Write-Host "[*] Adding $localBin to your permanent Environment PATH..." -ForegroundColor Yellow
    $newUserPath = ($pathList + $localBin) -join ';'
    [System.Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
    # Update active session path as well so they can run it immediately without restarting
    $env:PATH = "$localBin;" + $env:PATH
} else {
    # Ensure active session PATH has it in case they started this session without it
    if ($env:PATH -notlike "*$localBin*") {
        $env:PATH = "$localBin;" + $env:PATH
    }
}

# 3. Run installation via uv
Write-Host "[*] Installing FEDERaiDE with extras (audio, vision) on standardized Python 3.13..." -ForegroundColor Yellow
uv tool install --refresh --python 3.13 "federaide[audio,vision]"

Write-Host "======================================================================" -ForegroundColor Green
Write-Host " 🎉 Windows installation complete!" -ForegroundColor Green
Write-Host " Please restart your terminal/shell to ensure paths are updated." -ForegroundColor Green
Write-Host "======================================================================" -ForegroundColor Green