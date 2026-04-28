# -*- mode: python ; coding: utf-8 -*-
#
# Cross-platform PyInstaller spec used by .github/workflows/release.yml.
# Builds a single-file executable that embeds:
#   - The Python runtime + tkinter
#   - extract_frames_with_gps_similarity.py + main_packaged.py
#   - opencv-python + numpy (so Adaptive mode works without a Python install)
#   - bin/ffmpeg, bin/ffprobe, bin/exiftool (.exe on Windows)
#
# The CI workflow populates ./cross_build/bin/ with the right binaries for
# the current OS before invoking pyinstaller.

import sys
from pathlib import Path

spec_dir = Path(SPECPATH).resolve()
project_dir = spec_dir.parent  # holds extract_frames_with_gps_similarity.py
bin_dir = spec_dir / "bin"

is_windows = sys.platform.startswith("win")
exe_suffix = ".exe" if is_windows else ""

required_bins = [f"ffmpeg{exe_suffix}", f"ffprobe{exe_suffix}",
                 f"exiftool{exe_suffix}"]

binaries = []
for name in required_bins:
    p = bin_dir / name
    if p.exists():
        binaries.append((str(p), "bin"))

# Bundle exiftool's perl libs folder if present (Windows portable build only).
et_files = bin_dir / "exiftool_files"
datas = []
if et_files.is_dir():
    datas.append((str(et_files), "bin/exiftool_files"))


a = Analysis(
    ["main_packaged.py"],
    pathex=[str(project_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["cv2", "numpy"],
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
    name="extract-frames",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
