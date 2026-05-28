"""Make bundled OpenCV CUDA DLLs visible before cv2 is imported."""

import os
import sys
from pathlib import Path


if sys.platform.startswith("win") and hasattr(os, "add_dll_directory"):
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    for candidate in (base, base / "bin"):
        if candidate.exists():
            os.add_dll_directory(str(candidate))
