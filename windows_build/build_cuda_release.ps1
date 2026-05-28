param(
    [string]$OpenCVInstallDir = "",
    [string]$AssetName = "extract-frames-windows-x64-cuda11.8-sm86.zip",
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not $OpenCVInstallDir) {
    $OpenCVInstallDir = Join-Path $RepoRoot ".opencv_cuda_build\install_cuda_py310"
}

$OpenCVBinDir = Join-Path $OpenCVInstallDir "x64\vc16\bin"
if (-not (Test-Path $OpenCVBinDir)) {
    throw "OpenCV CUDA bin directory not found: $OpenCVBinDir"
}
if (-not $env:CUDA_PATH) {
    throw "CUDA_PATH is not set. Install CUDA Toolkit or set CUDA_PATH first."
}

Write-Host "== Installing Python packaging deps =="
& $PythonExe -m pip install --upgrade pyinstaller numpy piexif

Write-Host "== Verifying cv2.cuda before packaging =="
$env:OPENCV_CUDA_BIN_DIR = $OpenCVBinDir
$env:PATH = "$OpenCVBinDir;$env:CUDA_PATH\bin;$env:PATH"
& $PythonExe -c "import cv2; print(cv2.__version__); assert cv2.cuda.getCudaEnabledDeviceCount() > 0; assert hasattr(cv2, 'cuda_ORB'); print([l for l in cv2.getBuildInformation().splitlines() if 'NVIDIA CUDA' in l][0])"

$CrossBuild = Join-Path $RepoRoot "cross_build"
$BinDir = Join-Path $CrossBuild "bin"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

if (-not (Test-Path (Join-Path $BinDir "ffmpeg.exe")) -or
    -not (Test-Path (Join-Path $BinDir "ffprobe.exe"))) {
    Write-Host "== Fetching ffmpeg/ffprobe =="
    $FfZip = Join-Path $env:TEMP "ffmpeg-release-essentials.zip"
    $FfExtract = Join-Path $env:TEMP "ffmpeg-release-essentials"
    Invoke-WebRequest "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" `
        -OutFile $FfZip -UseBasicParsing
    if (Test-Path $FfExtract) {
        Remove-Item -Recurse -Force $FfExtract
    }
    Expand-Archive -Force $FfZip $FfExtract
    Copy-Item -Force (Get-ChildItem $FfExtract -Recurse -Filter ffmpeg.exe | Select-Object -First 1).FullName `
        (Join-Path $BinDir "ffmpeg.exe")
    Copy-Item -Force (Get-ChildItem $FfExtract -Recurse -Filter ffprobe.exe | Select-Object -First 1).FullName `
        (Join-Path $BinDir "ffprobe.exe")
}

Write-Host "== Running PyInstaller =="
Push-Location $CrossBuild
try {
    & $PythonExe -m PyInstaller build.spec --clean --noconfirm
}
finally {
    Pop-Location
}

$DistDir = Join-Path $CrossBuild "dist"
$BundleDir = Join-Path $DistDir "extract-frames"
$AssetPath = Join-Path $DistDir $AssetName
if (-not (Test-Path $BundleDir)) {
    throw "Expected PyInstaller onedir output not found: $BundleDir"
}
if (Test-Path $AssetPath) {
    Remove-Item -Force $AssetPath
}

Write-Host "== Creating zip asset =="
Compress-Archive -Path $BundleDir -DestinationPath $AssetPath -Force
Get-Item $AssetPath | Format-List FullName,Length

Write-Host "CUDA release asset: $AssetPath"
