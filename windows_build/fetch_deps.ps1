# Downloads ffmpeg.exe, ffprobe.exe and exiftool.exe into ./bin/.
# Invoked automatically by build.bat on the first build. Safe to re-run; it
# will overwrite the files in place.
#
# Sources:
#   - ffmpeg (Windows release essentials build by Gyan Doshi, www.gyan.dev)
#   - exiftool (Phil Harvey, exiftool.org)
#
# Run manually with:
#   powershell -ExecutionPolicy Bypass -File fetch_deps.ps1

$ErrorActionPreference = "Stop"

# Use TLS 1.2+ (Windows PowerShell 5.1 defaults to TLS 1.0 which fails on
# modern hosts).
[Net.ServicePointManager]::SecurityProtocol =
    [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13

# Operate inside the script's directory regardless of where it's invoked.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $scriptDir

$binDir = Join-Path $scriptDir "bin"
if (-not (Test-Path $binDir)) {
    New-Item -ItemType Directory -Path $binDir | Out-Null
}

function Download-And-Extract {
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$ZipPath,
        [Parameter(Mandatory)][string]$ExtractDir
    )
    Write-Host "  URL: $Url"
    Invoke-WebRequest -Uri $Url -OutFile $ZipPath -UseBasicParsing
    if (Test-Path $ExtractDir) { Remove-Item -Recurse -Force $ExtractDir }
    Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir -Force
}


# --- ffmpeg + ffprobe -------------------------------------------------------
Write-Host ""
Write-Host "[1/2] Fetching ffmpeg + ffprobe (release-essentials build)..."

$ffUrl        = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
$ffZip        = Join-Path $env:TEMP "ffmpeg-release-essentials.zip"
$ffExtractDir = Join-Path $env:TEMP "ffmpeg-extract"

Download-And-Extract -Url $ffUrl -ZipPath $ffZip -ExtractDir $ffExtractDir

$ffmpegExe  = Get-ChildItem -Path $ffExtractDir -Recurse -Filter "ffmpeg.exe"  | Select-Object -First 1
$ffprobeExe = Get-ChildItem -Path $ffExtractDir -Recurse -Filter "ffprobe.exe" | Select-Object -First 1
if (-not $ffmpegExe -or -not $ffprobeExe) {
    throw "Could not locate ffmpeg.exe / ffprobe.exe in the downloaded archive."
}
Copy-Item -Force $ffmpegExe.FullName  (Join-Path $binDir "ffmpeg.exe")
Copy-Item -Force $ffprobeExe.FullName (Join-Path $binDir "ffprobe.exe")


# --- exiftool ---------------------------------------------------------------
Write-Host ""
Write-Host "[2/2] Fetching exiftool..."

# exiftool.org publishes the current version number in /ver.txt; use it to
# build the exact zip URL so we always grab the latest release.
$etVer = (Invoke-WebRequest -Uri "https://exiftool.org/ver.txt" -UseBasicParsing).Content.Trim()
Write-Host "  exiftool version: $etVer"

$etZipName    = "exiftool-${etVer}_64.zip"
$etUrl        = "https://exiftool.org/$etZipName"
$etZip        = Join-Path $env:TEMP $etZipName
$etExtractDir = Join-Path $env:TEMP "exiftool-extract"

Download-And-Extract -Url $etUrl -ZipPath $etZip -ExtractDir $etExtractDir

# Phil Harvey ships the standalone exe as `exiftool(-k).exe` (the -k suffix
# makes it pause on exit when double-clicked). Rename to plain exiftool.exe
# for command-line use.
$etExe = Get-ChildItem -Path $etExtractDir -Recurse -Filter "exiftool*.exe" | Select-Object -First 1
if (-not $etExe) {
    throw "Could not locate exiftool(-k).exe in the downloaded archive."
}
Copy-Item -Force $etExe.FullName (Join-Path $binDir "exiftool.exe")

# Newer exiftool builds ship a companion folder `exiftool_files\` with Perl
# libraries that the exe loads at runtime. Bundle it too if present.
$etFiles = Get-ChildItem -Path $etExtractDir -Recurse -Directory -Filter "exiftool_files" | Select-Object -First 1
if ($etFiles) {
    $dstFiles = Join-Path $binDir "exiftool_files"
    if (Test-Path $dstFiles) { Remove-Item -Recurse -Force $dstFiles }
    Copy-Item -Recurse -Force $etFiles.FullName $dstFiles
    Write-Host "  (copied supporting exiftool_files\)"
}


# --- cleanup + summary ------------------------------------------------------
Remove-Item -Force -ErrorAction SilentlyContinue $ffZip, $etZip
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $ffExtractDir, $etExtractDir

Write-Host ""
Write-Host "Dependencies ready in bin\:"
Get-ChildItem $binDir | ForEach-Object {
    if ($_.PSIsContainer) {
        Write-Host ("  {0}\" -f $_.Name)
    } else {
        Write-Host ("  {0}  ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB))
    }
}
