# DJI Frame Extractor with GPS EXIF

A cross-platform GUI/CLI tool that extracts JPEG frames from DJI drone
videos, embeds per-frame GPS coordinates from the matching `.SRT` sidecar
into EXIF, and writes the result as a single sequentially-numbered folder
(`1.jpg`, `2.jpg`, ...) — ready to feed into COLMAP, 3D Gaussian
Splatting, photogrammetry pipelines, etc.

Supports macOS, Linux, Windows. Bundled `.exe` / `.app` / Linux binary
builds are attached to each [GitHub Release](../../releases) — no Python
install required for end users.

---

## Versions

### v0.3.1 — Windows SmartScreen mitigations

* Windows build switched from PyInstaller `--onefile` to `--onedir`,
  shipped as a zip. Defender / SmartScreen flag the self-extracting
  `--onefile` pattern much more aggressively than a plain folder of DLLs;
  switching dramatically reduces the rate at which the exe is blocked.
* Embedded a Windows `VSVersionInfo` resource so the SmartScreen dialog
  shows the real product name and publisher (`NJULiuYvXi /
  DJI Frame Extractor`) instead of "Unknown publisher".
* README documents the bypass for Windows SmartScreen and macOS
  Gatekeeper for the (still unsigned) downloads.

### v0.3.0 — Adaptive (similarity-based) extraction

Script: [`extract_frames_with_gps_similarity.py`](extract_frames_with_gps_similarity.py)

* New extraction mode **Adaptive (similarity)**: keeps a frame whenever
  the geometric overlap with the last-kept frame drops into a target band
  (default ~30% ± 5%). Solves the "drone flies fast vs. slow" problem
  where fixed-interval extraction either wastes frames or misses motion.
* Two user-selectable detectors:
  * **ORB + RANSAC homography** — fast binary descriptors, optional
    `cv2.cuda_ORB` GPU acceleration when OpenCV is built with CUDA.
  * **SIFT + RANSAC homography** — slower, more robust to scale/rotation.
* Geometric overlap measured by Lowe-ratio matching →
  `cv2.findHomography(RANSAC)` → corner warp →
  `cv2.intersectConvexConvex` → intersection-area / frame-area.
* Per-keep logging shows source frame index, output filename, measured
  overlap, and gap to last-kept frame.
* Cross-platform `cv2.cuda` auto-detection (silently falls back to CPU).
* Fixed-interval mode (v0.2 behavior) still available via the mode
  dropdown.

### v0.2.0 — Cross-platform GPU decode hwaccel

Script: [`extract_frames_with_gps_hwaccel.py`](extract_frames_with_gps_hwaccel.py)

* Added a *Use GPU acceleration (auto-detect)* checkbox (default OFF).
  Probes OS + ffmpeg build + actual hardware and picks the best
  available decoder:
  * macOS → **VideoToolbox**
  * Linux → **cuda** (NVIDIA) / **vaapi** (AMD/Intel) / **vdpau**
  * Windows → **cuda** (NVIDIA) / **d3d11va** / **qsv** (Intel) / **dxva2**
* Status label + Re-detect button surface what was picked.
* GPU is used for decode only; Lanczos scaling and JPEG encoding stay on
  CPU (no portable GPU JPEG encoder).
* PyInstaller assets (`windows_build/`) for producing a single
  `extract_frames_hwaccel.exe` with ffmpeg/ffprobe/exiftool bundled inside.

### v0.1.0 — Initial release

Script: [`extract_frames_with_gps.py`](extract_frames_with_gps.py)

* Tkinter GUI: pick input folder of `.MP4` + `.SRT`, pick output folder,
  extract every Nth frame.
* Per-frame ffmpeg progress bar (`-progress pipe:1`) with cancel button.
* Output resolution presets (4K / 1440p / 1080p / 720p / 540p) plus
  custom `WxH`, Lanczos resampling.
* JPEG quality 1–31 (`ffmpeg -q:v`).
* GPS lat/lon/alt parsed from the DJI `.SRT` and written to EXIF
  (`GPSLatitude`, `GPSLongitude`, `GPSAltitude` + refs) via
  `exiftool -csv=` batch.
* Single output folder, contiguous numbering across multiple input
  videos (`-start_number` chained per file).

---

## Quick start

### Pre-built binaries

Download from the latest [Release](../../releases/latest):

* **Windows (x64)** — `extract-frames-windows-x64.zip`. Extract anywhere,
  then run `extract-frames.exe` inside. ffmpeg / ffprobe / exiftool are
  bundled.
* **macOS (Apple Silicon)** — `extract-frames-macos-arm64`. Run with
  `chmod +x extract-frames-macos-arm64 && ./extract-frames-macos-arm64`.
  Requires `brew install ffmpeg exiftool` on the host.
* **Linux (x86_64)** — `extract-frames-linux-x86_64`. Run with
  `chmod +x extract-frames-linux-x86_64 && ./extract-frames-linux-x86_64`.
  Requires `apt install ffmpeg libimage-exiftool-perl`.

#### Windows SmartScreen warning ("Windows protected your PC")

The `.exe` is **not code-signed** (signing certificates cost money).
Microsoft Defender SmartScreen will show a blue warning the first time
you run it. To bypass:

1. Click **More info** in the warning dialog.
2. Click **Run anyway**.

This decision is remembered — subsequent runs are silent. The exe is
built reproducibly from this repo by [GitHub Actions](.github/workflows/release.yml);
you can also build it yourself from source (see below) and skip the
download entirely.

If your IT policy blocks SmartScreen overrides:
- Right-click the zip / exe → Properties → tick **Unblock** before running.
- Or run from source (`pip install` flow below).
- Or build the exe locally with `windows_build/build.bat`.

#### macOS Gatekeeper warning ("cannot be opened because the developer cannot be verified")

Same situation — unsigned binary. To bypass:

```bash
xattr -d com.apple.quarantine extract-frames-macos-arm64
chmod +x extract-frames-macos-arm64
./extract-frames-macos-arm64
```

### Run from source

```bash
git clone https://github.com/NJULiuYvXi/dji-frame-extractor.git
cd dji-frame-extractor

# Mac/Linux
brew install ffmpeg exiftool             # or: apt install ffmpeg libimage-exiftool-perl
pip install opencv-python numpy           # only needed for Adaptive mode

python extract_frames_with_gps_similarity.py
```

### CLI

```bash
# Fixed interval (every 30th frame, scaled to 1080p, q=2, GPU decode)
python extract_frames_with_gps_similarity.py --cli fixed \
    /path/to/videos /path/to/out 30 1920x1080 2 --gpu

# Adaptive (target overlap 30%, ±5%, ORB)
python extract_frames_with_gps_similarity.py --cli adaptive \
    /path/to/videos /path/to/out 30 5 ORB 1920x1080 2 --cv2-cuda
```

---

## How adaptive overlap works

For each frame *N* (downsampled to a long side of 720 px for speed) the
estimator:

1. Detects features (ORB or SIFT) and descriptors.
2. KNN-matches against the last-kept frame's descriptors and applies a
   Lowe ratio test (0.75).
3. Solves a RANSAC homography from current → kept-frame coordinates.
4. Warps the current frame's corner rectangle through the homography and
   intersects it with the kept-frame's rectangle
   (`cv2.intersectConvexConvex`).
5. Reports the intersection-area / frame-area ratio as overlap.
6. Keeps the current frame iff overlap ≤ target + tolerance, then
   re-anchors the chain on it.

This handles variable drone speed and varying subject distance naturally:
when the drone hovers, the overlap stays high and frames are skipped;
when it sweeps quickly past a façade, overlap drops fast and frames are
sampled densely.

---

## Privacy note

`.SRT` sidecars contain precise GPS coordinates of every flight. The
repo's `.gitignore` excludes `*.MP4`, `*.SRT`, and `frames*/` so flight
data is never accidentally pushed.

---

## Repository layout

```
.
├── extract_frames_with_gps.py            # v0.1 (CPU-only fixed interval)
├── extract_frames_with_gps_hwaccel.py    # v0.2 (+ GPU decode hwaccel)
├── extract_frames_with_gps_similarity.py # v0.3 (+ adaptive ORB/SIFT)
├── windows_build/                        # legacy Windows-only PyInstaller
├── cross_build/                          # cross-platform PyInstaller (CI)
└── .github/workflows/release.yml         # builds Win/Mac/Linux on tag push
```
