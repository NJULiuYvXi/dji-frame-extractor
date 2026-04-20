# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for `extract_frames_with_gps_hwaccel` on Windows.
#
# Builds a single .exe that bundles:
#   - The Python runtime + tkinter
#   - The main script and its wrapper (main_win.py)
#   - bin\ffmpeg.exe, bin\ffprobe.exe, bin\exiftool.exe
#
# Run with:   python -m PyInstaller build.spec --clean --noconfirm
# Output:     dist\extract_frames_hwaccel.exe
#
# The resulting architecture (x64 vs x86) matches whichever Python is used
# to run PyInstaller. Use 64-bit Python for a 64-bit exe, or 32-bit Python
# for a 32-bit exe. No cross-compilation.

from pathlib import Path

spec_dir = Path(SPECPATH).resolve()
project_dir = spec_dir.parent  # holds extract_frames_with_gps_hwaccel.py
bin_dir = spec_dir / "bin"

# Sanity check: the bundled binaries must be present before building.
required_bins = ["ffmpeg.exe", "ffprobe.exe", "exiftool.exe"]
missing = [b for b in required_bins if not (bin_dir / b).exists()]
if missing:
    raise SystemExit(
        "ERROR: Missing required binaries in windows_build/bin:\n"
        + "\n".join(f"  - {b}" for b in missing)
        + "\nRun `powershell -ExecutionPolicy Bypass -File fetch_deps.ps1` first."
    )

# Tell PyInstaller to copy each binary into the bundle's `bin/` subfolder,
# where main_win.py will find it via sys._MEIPASS\bin.
binaries = [(str(bin_dir / b), "bin") for b in required_bins]


a = Analysis(
    ["main_win.py"],
    pathex=[str(project_dir)],   # so we can import extract_frames_with_gps_hwaccel
    binaries=binaries,
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="extract_frames_hwaccel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX disabled: it double-compresses already-compressed ffmpeg.exe,
    # slows startup, and can trip Windows Defender SmartScreen.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI app -> no black console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,       # native to the building Python
    codesign_identity=None,
    entitlements_file=None,
    icon=None,              # add an .ico path here if you want a custom icon
)
