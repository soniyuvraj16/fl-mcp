# Copies the FLMCP controller script into FL Studio's Hardware folder.
#
# FL Studio scans  <Documents>\Image-Line\FL Studio\Settings\Hardware\<Folder>\device_*.py
# at startup and lists each script (by its "# name=" line) in the Controller
# type dropdown of Options > MIDI Settings.  Run this once, then restart FL
# (or hit "Refresh device list" / toggle Enable on the device).
#
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\install_controller.ps1
#         (add -FLSettings "D:\somewhere\FL Studio\Settings" if your Documents
#          folder is not the default or OneDrive-redirected one)

param(
    [string]$FLSettings = ""
)

$ErrorActionPreference = "Stop"

$repo   = Split-Path -Parent $PSScriptRoot
$source = Join-Path $repo "fl_controller\device_FLMCP.py"
if (-not (Test-Path $source)) { throw "Controller script not found at $source" }

if ($FLSettings -eq "") {
    # GetFolderPath respects OneDrive "Known Folder Move" redirection.
    $docs = [Environment]::GetFolderPath("MyDocuments")
    $FLSettings = Join-Path $docs "Image-Line\FL Studio\Settings"
}

$targetDir  = Join-Path $FLSettings "Hardware\FLMCP"
$targetFile = Join-Path $targetDir "device_FLMCP.py"

if (-not (Test-Path (Split-Path $FLSettings -Parent))) {
    Write-Warning "FL Studio settings folder not found at $FLSettings. Has FL Studio been launched at least once on this machine? Creating it anyway."
}

New-Item -ItemType Directory -Force -Path $targetDir | Out-Null

if (Test-Path $targetFile) {
    $backup = "$targetFile.bak"
    Copy-Item $targetFile $backup -Force
    Write-Host "Backed up existing script to $backup"
}

Copy-Item $source $targetFile -Force
Write-Host "Installed controller to: $targetFile"
Write-Host ""
Write-Host "Next: start (or restart) FL Studio, open Options > MIDI Settings, select"
Write-Host "FLMCP_In in the Input list, enable it, set Controller type = FLMCP, and give"
Write-Host "FLMCP_In and FLMCP_Out the same Port number. Then View > Script output."
