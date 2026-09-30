param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPath = Join-Path $ProjectRoot ".venv"
$PythonPath = Join-Path $VenvPath "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $PythonPath)) {
    py -3.12 -m venv $VenvPath
}

if (-not $SkipInstall) {
    & $PythonPath -m pip install --upgrade pip
    & $PythonPath -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
}

& $PythonPath -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name "RoadStitcher" `
    --icon (Join-Path $ProjectRoot "assets\road_stitcher_v2.ico") `
    --add-data "$ProjectRoot\road_stitcher\assets\road_stitcher_icon_v2.png;road_stitcher\assets" `
    --hidden-import analyze_aerial_overlap `
    --hidden-import optimize_aerial_road_stitch `
    (Join-Path $ProjectRoot "run_road_stitcher.py")

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed. Close RoadStitcher and try again."
}

$ReleaseDir = Join-Path $ProjectRoot "dist\RoadStitcher"
Copy-Item -LiteralPath (Join-Path $ProjectRoot "README.md") `
    -Destination $ReleaseDir -Force
$ArchivePath = Join-Path $ProjectRoot "dist\RoadStitcher-0.3.8-windows-x64.zip"
Compress-Archive -LiteralPath $ReleaseDir -DestinationPath $ArchivePath -Force

Write-Host ""
Write-Host "Build complete: $ProjectRoot\dist\RoadStitcher"
Write-Host "Portable archive: $ArchivePath"
