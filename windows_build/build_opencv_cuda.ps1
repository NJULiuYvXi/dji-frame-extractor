param(
    [string]$OpenCVVersion = "4.10.0",
    [string]$CudaArchBin = "8.6",
    [string]$WorkRoot = "",
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not $WorkRoot) {
    $WorkRoot = Join-Path $RepoRoot ".opencv_cuda_build"
}

$OpenCVSrc = Join-Path $WorkRoot "opencv"
$ContribSrc = Join-Path $WorkRoot "opencv_contrib"
$BuildDir = Join-Path $WorkRoot "build_cuda_py310"
$InstallDir = Join-Path $WorkRoot "install_cuda_py310"

Write-Host "== Installing Python build tools =="
& $PythonExe -m pip install --upgrade pip setuptools wheel cmake ninja numpy piexif pyinstaller

Write-Host "== Resolving Python paths =="
$PyInfoJson = & $PythonExe -c "import json, numpy, pathlib, sys, sysconfig; root=pathlib.Path(sys.executable).parent; print(json.dumps({'exe':sys.executable,'include':sysconfig.get_path('include'),'lib':str(root/'libs'/('python'+str(sys.version_info.major)+str(sys.version_info.minor)+'.lib')),'numpy':numpy.get_include()}))"
$PyInfo = $PyInfoJson | ConvertFrom-Json

Write-Host "== Fetching OpenCV sources =="
New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null
if (-not (Test-Path $OpenCVSrc)) {
    git clone --depth 1 --branch $OpenCVVersion https://github.com/opencv/opencv.git $OpenCVSrc
}
if (-not (Test-Path $ContribSrc)) {
    git clone --depth 1 --branch $OpenCVVersion https://github.com/opencv/opencv_contrib.git $ContribSrc
}

$VsDevCmd = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2019\Community\Common7\Tools\VsDevCmd.bat"
if (-not (Test-Path $VsDevCmd)) {
    throw "Visual Studio 2019 VsDevCmd.bat not found: $VsDevCmd"
}

$CMakeArgs = @(
    "-S", ($OpenCVSrc -replace "\\", "/"),
    "-B", ($BuildDir -replace "\\", "/"),
    "-G", "Ninja",
    "-D", "CMAKE_BUILD_TYPE=Release",
    "-D", "CMAKE_INSTALL_PREFIX=$(($InstallDir -replace '\\', '/'))",
    "-D", "OPENCV_EXTRA_MODULES_PATH=$(($ContribSrc -replace '\\', '/'))/modules",
    "-D", "BUILD_LIST=core,imgproc,imgcodecs,videoio,calib3d,features2d,flann,cudev,cudaarithm,cudawarping,cudafeatures2d,cudaimgproc,cudafilters,python3",
    "-D", "WITH_CUDA=ON",
    "-D", "CUDA_ARCH_BIN=$CudaArchBin",
    "-D", "CUDA_ARCH_PTX=",
    "-D", "OPENCV_DNN_CUDA=OFF",
    "-D", "BUILD_opencv_world=OFF",
    "-D", "BUILD_SHARED_LIBS=ON",
    "-D", "BUILD_TESTS=OFF",
    "-D", "BUILD_PERF_TESTS=OFF",
    "-D", "BUILD_EXAMPLES=OFF",
    "-D", "BUILD_opencv_apps=OFF",
    "-D", "BUILD_JAVA=OFF",
    "-D", "BUILD_opencv_python2=OFF",
    "-D", "BUILD_opencv_python3=ON",
    "-D", "BUILD_opencv_gapi=OFF",
    "-D", "PYTHON3_EXECUTABLE=$(($PyInfo.exe -replace '\\', '/'))",
    "-D", "PYTHON3_INCLUDE_DIR=$(($PyInfo.include -replace '\\', '/'))",
    "-D", "PYTHON3_LIBRARY=$(($PyInfo.lib -replace '\\', '/'))",
    "-D", "PYTHON3_NUMPY_INCLUDE_DIRS=$(($PyInfo.numpy -replace '\\', '/'))"
)

Write-Host "== Configuring OpenCV CUDA =="
cmd /c "call `"$VsDevCmd`" -arch=x64 -host_arch=x64 && cmake $($CMakeArgs -join ' ')"

Write-Host "== Building OpenCV CUDA =="
cmd /c "call `"$VsDevCmd`" -arch=x64 -host_arch=x64 && cmake --build `"$BuildDir`" --config Release --parallel $env:NUMBER_OF_PROCESSORS"

Write-Host "== Installing OpenCV CUDA =="
cmd /c "call `"$VsDevCmd`" -arch=x64 -host_arch=x64 && cmake --install `"$BuildDir`" --config Release"

Write-Host "== Verifying cv2.cuda =="
$env:PATH = "$(Join-Path $InstallDir 'x64\vc16\bin');$env:CUDA_PATH\bin;$env:PATH"
& $PythonExe -c "import cv2; print(cv2.__version__); print(cv2.cuda.getCudaEnabledDeviceCount()); print(hasattr(cv2, 'cuda_ORB')); print([l for l in cv2.getBuildInformation().splitlines() if 'NVIDIA CUDA' in l][0])"

Write-Host "OpenCV CUDA install: $InstallDir"
