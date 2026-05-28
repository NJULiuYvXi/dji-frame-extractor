# -*- mode: python ; coding: utf-8 -*-
#
# Cross-platform PyInstaller spec used by .github/workflows/release.yml.
#
# On Windows we build in --onedir mode (a folder shipped as a .zip) and
# embed a VSVersionInfo resource. Both reduce the rate at which Microsoft
# Defender SmartScreen blocks the unsigned binary: onedir avoids the
# self-extracting-archive heuristic that flags PyInstaller --onefile, and
# the version block gives the SmartScreen dialog a real product name +
# publisher instead of "Unknown publisher".
#
# On macOS / Linux we keep --onefile (a single executable is more
# convenient and neither OS has SmartScreen).
#
# The CI workflow populates ./cross_build/bin/ with the right binaries
# before invoking pyinstaller.

import sys
import os
from pathlib import Path

spec_dir = Path(SPECPATH).resolve()
project_dir = spec_dir.parent  # holds extract_frames_with_gps_similarity.py
bin_dir = spec_dir / "bin"

is_windows = sys.platform.startswith("win")
exe_suffix = ".exe" if is_windows else ""

required_bins = [f"ffmpeg{exe_suffix}", f"ffprobe{exe_suffix}"]

binaries = []
for name in required_bins:
    p = bin_dir / name
    if p.exists():
        binaries.append((str(p), "bin"))

if is_windows:
    opencv_cuda_bin = os.environ.get("OPENCV_CUDA_BIN_DIR")
    if opencv_cuda_bin:
        for dll in sorted(Path(opencv_cuda_bin).glob("*.dll")):
            binaries.append((str(dll), "."))

    cuda_bin = Path(os.environ.get("CUDA_PATH", "")) / "bin"
    cuda_patterns = [
        "cudart64_*.dll",
        "cublas64_*.dll",
        "cublasLt64_*.dll",
        "cufft64_*.dll",
        "npp*64_*.dll",
    ]
    if cuda_bin.exists():
        for pattern in cuda_patterns:
            for dll in sorted(cuda_bin.glob(pattern)):
                binaries.append((str(dll), "."))

datas = []

# VSVersionInfo for Windows -- file generated alongside this spec.
version_file = spec_dir / "windows_version.txt"
version_arg = str(version_file) if (is_windows and version_file.exists()) else None


a = Analysis(
    ["main_packaged.py"],
    pathex=[str(project_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["cv2", "numpy", "piexif"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["opencv_cuda_runtime_hook.py"] if is_windows else [],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)


if is_windows:
    # --- Windows: --onedir (folder) so the workflow can zip it ---
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="extract-frames",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=None,
        version=version_arg,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="extract-frames",
    )
else:
    # --- macOS / Linux: --onefile single binary ---
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
