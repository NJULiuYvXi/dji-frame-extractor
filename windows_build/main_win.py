"""
PyInstaller entry-point wrapper for the Windows build.

At runtime, prepend the bundled `bin/` directory (containing ffmpeg.exe,
ffprobe.exe and exiftool.exe) to PATH so that the main app's shutil.which()
lookups find them. Works for both one-file (sys._MEIPASS = temp extraction
dir) and one-folder (sys.executable's directory) PyInstaller modes, and also
when running this script directly via `python main_win.py` if a sibling
`bin/` folder is present.
"""

import os
import sys
from pathlib import Path


def _setup_bundled_binaries() -> None:
    # When frozen by PyInstaller, sys._MEIPASS points to the extracted bundle
    # dir (one-file) or the executable's dir (one-folder). Prefer _MEIPASS.
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        else:
            base = Path(sys.executable).parent
    else:
        # Running the wrapper as a plain script (useful for local debugging).
        base = Path(__file__).resolve().parent

    bin_dir = base / "bin"
    if bin_dir.is_dir():
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


_setup_bundled_binaries()

# Import the real app. Inside the PyInstaller bundle the main script is on
# sys.path thanks to `pathex` in build.spec. When this wrapper is run as a
# plain script (for local debugging), we help Python find the parent folder.
if not getattr(sys, "frozen", False):
    parent = str(Path(__file__).resolve().parent.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)

from extract_frames_with_gps_hwaccel import main  # noqa: E402


if __name__ == "__main__":
    main()
