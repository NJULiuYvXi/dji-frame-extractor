"""
Cross-platform PyInstaller entry-point wrapper.

Used by the CI release workflow (.github/workflows/release.yml) to build
single-file executables for Windows / macOS / Linux. At runtime, it
prepends the bundled `bin/` dir (containing ffmpeg, ffprobe, exiftool --
with or without `.exe`) to PATH so the main app's shutil.which() lookups
resolve to bundled binaries.

Works for:
  - PyInstaller --onefile  (sys._MEIPASS = temp extraction dir)
  - PyInstaller --onedir   (sys.executable's parent)
  - Plain `python main_packaged.py`  (sibling bin/ if present)
"""

import os
import sys
from pathlib import Path


def _setup_bundled_binaries() -> None:
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        else:
            base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent

    bin_dir = base / "bin"
    if bin_dir.is_dir():
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


_setup_bundled_binaries()

# Make sure the project root is on sys.path when running as a plain script
# (CI invokes pyinstaller from the repo root so this is normally a no-op).
if not getattr(sys, "frozen", False):
    parent = str(Path(__file__).resolve().parent.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)

from extract_frames_with_gps_similarity import main  # noqa: E402


if __name__ == "__main__":
    main()
