#!/usr/bin/env python3
"""
DJI drone video -> sequential JPEG frames with GPS EXIF metadata.

Similarity-based variant. Adds adaptive frame extraction for 3D
reconstruction (Gaussian splatting / photogrammetry / COLMAP). Two extraction
modes are user-selectable:

  Fixed interval     -- the existing every-Nth-frame ffmpeg pipeline.
  Adaptive (overlap) -- decode every frame and keep one whenever the
                        estimated overlap with the last-kept frame drops
                        into the target band (default ~30%, +/- 5%).

Two feature detectors are selectable for the adaptive mode:

  ORB + RANSAC homography
      Fast binary descriptors. Optionally GPU-accelerated via cv2.cuda
      (cv2.cuda_ORB) when OpenCV is built with CUDA support, e.g. on
      Windows + NVIDIA with a contrib CUDA build, or on Linux with
      `opencv-cuda`. The default opencv-python wheels ship CPU-only
      and will silently fall back.

  SIFT + RANSAC homography
      Slower but more robust to scale/rotation. CPU only (mainline cv2
      has no GPU SIFT).

Both methods use the same geometric overlap estimate: feature matches ->
RANSAC homography -> warp last frame's corners into current frame's
coordinate system -> intersect with current frame rectangle -> ratio of
the intersection area to frame area.

Cross-platform support:
  macOS / Linux / Windows. The decode hwaccel auto-detection (videotoolbox
  / cuda / vaapi / d3d11va / qsv / dxva2) still applies to the fixed-
  interval mode (it pipes through ffmpeg). The adaptive mode uses
  cv2.VideoCapture for per-frame access; OpenCV will internally use
  ffmpeg, but hwaccel of cv2.VideoCapture is platform-dependent and not
  configured here -- decode runs on CPU in adaptive mode. The cv2.cuda
  GPU path applies to ORB feature detection and matching only.

External dependencies:
  - ffmpeg / ffprobe / exiftool       (must be on PATH)
  - opencv-python or opencv-contrib-python (and numpy)
"""

import csv
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


# --- Optional cv2 / numpy imports (only required for adaptive mode) ---------
#
# Imported lazily so that the GUI starts and the fixed-interval mode works
# even on a machine where opencv-python is not installed.

def _try_import_cv2():
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
        return cv2, np, None
    except Exception as e:  # noqa: BLE001
        return None, None, e


# --- SRT parsing (same as hwaccel variant) ----------------------------------

SRT_BLOCK_RE = re.compile(
    r"SrtCnt\s*:\s*(\d+)"
    r".*?\[\s*latitude\s*:\s*([-\d.]+)\s*\]"
    r"\s*\[\s*longitude\s*:\s*([-\d.]+)\s*\]"
    r"\s*\[\s*altitude\s*:\s*([-\d.]+)\s*\]",
    re.DOTALL,
)

RES_RE = re.compile(r"^\s*(\d+)\s*[x×*]\s*(\d+)\s*$", re.IGNORECASE)


def parse_srt(srt_path: Path):
    try:
        text = srt_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    out = {}
    for m in SRT_BLOCK_RE.finditer(text):
        idx = int(m.group(1))
        out[idx] = (float(m.group(2)), float(m.group(3)), float(m.group(4)))
    return out


def find_srt(video_path: Path):
    for ext in (".SRT", ".srt", ".Srt"):
        p = video_path.with_suffix(ext)
        if p.exists():
            return p
    return None


def probe_frame_count(video_path: Path) -> int:
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-count_packets", "-show_entries", "stream=nb_read_packets",
                "-of", "csv=p=0", str(video_path),
            ],
            capture_output=True, text=True, check=True,
        )
        return int(r.stdout.strip() or 0)
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return 0


def parse_resolution(s: str):
    if not s:
        return None
    m = RES_RE.match(s)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


# --- ffmpeg decode hwaccel detection (same as hwaccel variant) --------------

def _list_ffmpeg_hwaccels() -> set:
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-hwaccels"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    out = set()
    for line in r.stdout.splitlines():
        line = line.strip()
        if line and not line.endswith(":"):
            out.add(line.lower())
    return out


def _has_nvidia_gpu() -> bool:
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        r = subprocess.run(
            ["nvidia-smi", "-L"], capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and "GPU" in r.stdout


def _has_dri_render_node() -> bool:
    for i in range(128, 192):
        if os.path.exists(f"/dev/dri/renderD{i}"):
            return True
    return False


def _vaapi_device_path() -> str:
    for i in range(128, 192):
        p = f"/dev/dri/renderD{i}"
        if os.path.exists(p):
            return p
    return "/dev/dri/renderD128"


def probe_hwaccel():
    """Decode hwaccel for ffmpeg (used by fixed-interval mode)."""
    available = _list_ffmpeg_hwaccels()
    if not available:
        return None
    system = platform.system()
    if system == "Darwin":
        candidates = [
            ("videotoolbox", ["-hwaccel", "videotoolbox"],
             "Apple VideoToolbox", lambda: True),
        ]
    elif system == "Linux":
        candidates = [
            ("cuda", ["-hwaccel", "cuda"],
             "NVIDIA CUDA / NVDEC", _has_nvidia_gpu),
            ("vaapi",
             ["-hwaccel", "vaapi", "-hwaccel_device", _vaapi_device_path()],
             "VA-API (AMD / Intel)", _has_dri_render_node),
            ("vdpau", ["-hwaccel", "vdpau"],
             "VDPAU (NVIDIA legacy)", _has_nvidia_gpu),
        ]
    elif system == "Windows":
        candidates = [
            ("cuda", ["-hwaccel", "cuda"],
             "NVIDIA CUDA / NVDEC", _has_nvidia_gpu),
            ("d3d11va", ["-hwaccel", "d3d11va"],
             "Direct3D 11 Video Acceleration", lambda: True),
            ("qsv", ["-hwaccel", "qsv"],
             "Intel Quick Sync Video", lambda: True),
            ("dxva2", ["-hwaccel", "dxva2"],
             "DirectX Video Acceleration 2", lambda: True),
        ]
    else:
        return None
    for name, args, desc, predicate in candidates:
        if name in available and predicate():
            return {"name": name, "args": args, "description": desc}
    return None


# --- cv2 CUDA detection (used by adaptive mode for ORB) ---------------------

def probe_cv2_cuda():
    """Return (available, info_string)."""
    cv2, np, err = _try_import_cv2()
    if cv2 is None:
        return False, f"opencv-python not installed ({err})"
    try:
        n = cv2.cuda.getCudaEnabledDeviceCount()
    except Exception:  # noqa: BLE001
        return False, "this OpenCV build has no CUDA module"
    if n <= 0:
        return False, "no CUDA-capable device usable by OpenCV"
    try:
        name = cv2.cuda.printCudaDeviceInfo  # presence check
        return True, f"cv2.cuda available ({n} device(s))"
    except Exception:  # noqa: BLE001
        return True, f"cv2.cuda available ({n} device(s))"


# --- Overlap estimator ------------------------------------------------------

class OverlapEstimator:
    """
    Compute the fraction of the previous frame that remains visible in the
    current frame, via feature matching + RANSAC homography + corner warp.

    Detector choices:
      "ORB"  -- cv2.ORB_create + BF Hamming matcher (CPU)
                cv2.cuda_ORB                         (GPU, when available)
      "SIFT" -- cv2.SIFT_create + BF L2 matcher     (CPU only)
    """

    def __init__(self, detector="ORB", prefer_cuda=False,
                 nfeatures=2000, downsample_long_side=720):
        cv2, np, err = _try_import_cv2()
        if cv2 is None:
            raise RuntimeError(
                "Adaptive mode requires opencv-python. Install with:\n"
                "    pip install opencv-python numpy\n"
                f"(import error: {err})"
            )
        self.cv2 = cv2
        self.np = np
        self.detector_name = detector.upper()
        self.downsample = int(downsample_long_side)

        cuda_ok = False
        if prefer_cuda and self.detector_name == "ORB":
            try:
                cuda_ok = cv2.cuda.getCudaEnabledDeviceCount() > 0
            except Exception:  # noqa: BLE001
                cuda_ok = False
        self.use_cuda = cuda_ok

        if self.detector_name == "ORB":
            if self.use_cuda:
                self._orb_g = cv2.cuda_ORB.create(nfeatures=nfeatures)
                self._matcher_g = (
                    cv2.cuda.DescriptorMatcher_createBFMatcher(cv2.NORM_HAMMING)
                )
            else:
                self._orb = cv2.ORB_create(nfeatures=nfeatures)
                self._matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        elif self.detector_name == "SIFT":
            try:
                self._sift = cv2.SIFT_create(nfeatures=nfeatures)
            except AttributeError as e:
                raise RuntimeError(
                    "SIFT not available in this OpenCV build. Install "
                    "opencv-contrib-python or upgrade opencv-python (>=4.4)."
                ) from e
            self._matcher = cv2.BFMatcher(cv2.NORM_L2)
        else:
            raise ValueError(f"Unknown detector: {detector!r}")

    @property
    def description(self):
        if self.detector_name == "ORB":
            return f"ORB ({'cv2.cuda GPU' if self.use_cuda else 'CPU'})"
        return "SIFT (CPU)"

    def _prep_gray(self, frame_bgr):
        cv2 = self.cv2
        h, w = frame_bgr.shape[:2]
        long_side = max(h, w)
        if long_side > self.downsample:
            s = self.downsample / long_side
            new_w, new_h = int(round(w * s)), int(round(h * s))
            frame_bgr = cv2.resize(
                frame_bgr, (new_w, new_h), interpolation=cv2.INTER_AREA
            )
        return cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)

    def features(self, frame_bgr):
        """Detect+describe; returns an opaque dict consumed by overlap()."""
        cv2 = self.cv2
        gray = self._prep_gray(frame_bgr)
        h, w = gray.shape[:2]
        if self.detector_name == "ORB" and self.use_cuda:
            gpu = cv2.cuda_GpuMat()
            gpu.upload(gray)
            kps_g, desc_g = self._orb_g.detectAndComputeAsync(gpu, None)
            kps = self._orb_g.convert(kps_g)
            return {
                "kp": kps, "desc_g": desc_g, "desc": None, "shape": (h, w),
            }
        if self.detector_name == "ORB":
            kps, desc = self._orb.detectAndCompute(gray, None)
        else:  # SIFT
            kps, desc = self._sift.detectAndCompute(gray, None)
        return {"kp": kps, "desc_g": None, "desc": desc, "shape": (h, w)}

    @staticmethod
    def _ratio_filter(matches, ratio=0.75):
        good = []
        for pair in matches:
            if len(pair) < 2:
                continue
            m, n = pair
            if m.distance < ratio * n.distance:
                good.append(m)
        return good

    def overlap(self, fa, fb):
        """
        Return the fraction of frame B's area that lies inside frame A.
        i.e. how much of B is also seen in A. 1.0 == identical view,
        0.0 == disjoint.
        """
        cv2 = self.cv2
        np = self.np
        if fa is None or fb is None:
            return 0.0

        # --- match descriptors ---
        try:
            if self.use_cuda:
                if fa["desc_g"] is None or fb["desc_g"] is None:
                    return 0.0
                if (fa["desc_g"].size().width == 0
                        or fb["desc_g"].size().width == 0):
                    return 0.0
                matches = self._matcher_g.knnMatch(fb["desc_g"], fa["desc_g"], k=2)
            else:
                if fa["desc"] is None or fb["desc"] is None:
                    return 0.0
                if len(fa["desc"]) < 4 or len(fb["desc"]) < 4:
                    return 0.0
                matches = self._matcher.knnMatch(fb["desc"], fa["desc"], k=2)
        except cv2.error:
            return 0.0

        good = self._ratio_filter(matches, ratio=0.75)
        if len(good) < 8:
            return 0.0

        kpA = fa["kp"]
        kpB = fb["kp"]

        def _pt(kp_list, idx):
            kp = kp_list[idx]
            # cuda_ORB.convert returns numpy array of [x,y,...] rows; cpu kps
            # are KeyPoint objects with .pt
            if hasattr(kp, "pt"):
                return kp.pt
            return (float(kp[0]), float(kp[1]))

        ptsA = np.float32([_pt(kpA, m.trainIdx) for m in good]).reshape(-1, 1, 2)
        ptsB = np.float32([_pt(kpB, m.queryIdx) for m in good]).reshape(-1, 1, 2)

        H, mask = cv2.findHomography(ptsB, ptsA, cv2.RANSAC, 3.0)
        if H is None:
            return 0.0

        hb, wb = fb["shape"]
        ha, wa = fa["shape"]
        cornersB = np.float32(
            [[0, 0], [wb, 0], [wb, hb], [0, hb]]
        ).reshape(-1, 1, 2)
        try:
            warped = cv2.perspectiveTransform(cornersB, H).reshape(-1, 2)
        except cv2.error:
            return 0.0
        rectA = np.float32([[0, 0], [wa, 0], [wa, ha], [0, ha]])

        try:
            inter_area, _ = cv2.intersectConvexConvex(warped, rectA)
        except cv2.error:
            return 0.0

        # Sanity: warped quad should be convex and not degenerate.
        # If it's twisted (not convex) or huge, treat as bad estimate.
        if inter_area <= 0:
            return 0.0
        # Normalize against B's full area (mapped via H, but we use B's
        # native pixel area as the denominator: ratio of B that matches A).
        b_area = float(wb * hb)
        if b_area <= 0:
            return 0.0
        ratio = float(inter_area) / b_area
        if ratio > 1.5:  # silly homography
            return 0.0
        return min(1.0, ratio)


# --- Fixed-interval extraction (ffmpeg pipeline, decode hwaccel applies) ----

def plan_video(video: Path, interval: int):
    srt_path = find_srt(video)
    gps_map = parse_srt(srt_path) if srt_path else {}
    max_src = max(gps_map) if gps_map else probe_frame_count(video)
    n_expected = 0
    if max_src > 0 and interval > 0:
        n_expected = (max_src + interval - 1) // interval
    return srt_path, gps_map, max_src, n_expected


def extract_video_fixed(
    video: Path, srt_path, gps_map, max_src,
    output_dir: Path, start_number: int,
    interval: int, resolution, jpeg_quality: int,
    hwaccel,
    base_progress: int, total_progress: int,
    log, set_progress, cancel_event: threading.Event,
):
    log(f"  SRT: {srt_path.name if srt_path else 'MISSING'} "
        f"({len(gps_map)} GPS entries)")
    if max_src <= 0:
        log("  Could not determine frame count; skipping.")
        return 0

    selected_src = list(range(1, max_src + 1, interval))
    if not selected_src:
        return 0

    vf_parts = []
    if interval > 1:
        vf_parts.append(f"select='not(mod(n\\,{interval}))'")
    if resolution is not None:
        vf_parts.append(f"scale={resolution[0]}:{resolution[1]}:flags=lanczos")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]
    if hwaccel:
        cmd += hwaccel["args"]
    cmd += ["-i", str(video)]
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
    if interval > 1:
        cmd += ["-fps_mode", "vfr"]
    cmd += [
        "-q:v", str(jpeg_quality),
        "-start_number", str(start_number),
        "-progress", "pipe:1", "-nostats",
        str(output_dir / "%d.jpg"),
    ]

    res_label = f"{resolution[0]}x{resolution[1]}" if resolution else "original"
    hw_label = hwaccel["name"] if hwaccel else "cpu"
    log(f"  [Fixed] Extracting ~{len(selected_src)} frames "
        f"(res: {res_label}, q={jpeg_quality}, decode: {hw_label})...")

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    def pump_stderr():
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                log(f"    ffmpeg: {line}")

    t_err = threading.Thread(target=pump_stderr, daemon=True)
    t_err.start()

    last_n = 0
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("frame="):
                try:
                    last_n = int(line.split("=", 1)[1])
                except ValueError:
                    pass
                set_progress(base_progress + last_n, total_progress)
            if cancel_event.is_set():
                proc.terminate()
                break
    finally:
        proc.wait()
        t_err.join(timeout=1.0)

    if cancel_event.is_set():
        return 0

    if proc.returncode != 0:
        hint = ""
        if hwaccel:
            hint = (f"  (Hint: '{hwaccel['name']}' decode failed; "
                    "turn GPU off and retry.)")
        raise RuntimeError(f"ffmpeg exited with code {proc.returncode}{hint}")

    written = 0
    for i in range(len(selected_src)):
        if (output_dir / f"{start_number + i}.jpg").exists():
            written = i + 1
        else:
            break

    log(f"  Wrote {written} JPEGs.")
    set_progress(base_progress + written, total_progress)

    _write_gps_exif(output_dir, start_number, written, selected_src, gps_map,
                    video.stem, log)
    return written


# --- Adaptive (similarity) extraction ---------------------------------------

def extract_video_adaptive(
    video: Path, srt_path, gps_map, max_src,
    output_dir: Path, start_number: int,
    target_overlap: float, tolerance: float,
    detector_name: str, prefer_cv2_cuda: bool,
    feature_long_side: int,
    resolution, jpeg_quality: int,
    base_progress: int, total_progress: int,
    log, set_progress, cancel_event: threading.Event,
):
    cv2, np, err = _try_import_cv2()
    if cv2 is None:
        raise RuntimeError(
            "Adaptive mode requires opencv-python.\n"
            "Install:  pip install opencv-python numpy\n"
            f"(import error: {err})"
        )

    log(f"  SRT: {srt_path.name if srt_path else 'MISSING'} "
        f"({len(gps_map)} GPS entries)")

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        log("  Could not open video with cv2.VideoCapture; skipping.")
        return 0

    nframes_cv = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or max_src

    estimator = OverlapEstimator(
        detector=detector_name, prefer_cuda=prefer_cv2_cuda,
        downsample_long_side=feature_long_side,
    )

    # cv2 IMWRITE_JPEG_QUALITY uses 0..100 (higher is better).
    # ffmpeg -q:v uses 1..31 (lower is better). Convert linearly.
    cv2_q = max(1, min(100, int(round(100 - (jpeg_quality - 1) * 80 / 30))))

    upper = float(target_overlap) + float(tolerance)
    lower = float(target_overlap) - float(tolerance)
    upper = min(0.99, max(0.05, upper))
    lower = min(0.95, max(0.01, lower))

    res_label = f"{resolution[0]}x{resolution[1]}" if resolution else "original"
    log(f"  [Adaptive] target overlap={target_overlap*100:.0f}% "
        f"(+/-{tolerance*100:.0f}%), detector={estimator.description}, "
        f"feat side={feature_long_side}px, res={res_label}, jpeg q={cv2_q}")

    written = 0
    src_indices_kept = []
    last_features = None
    last_kept_src = 0
    src_idx = 0
    last_overlap = 1.0

    def _save(frame, dst_idx):
        out_frame = frame
        if resolution is not None:
            out_frame = cv2.resize(
                frame, (resolution[0], resolution[1]),
                interpolation=cv2.INTER_LANCZOS4,
            )
        out_path = output_dir / f"{dst_idx}.jpg"
        cv2.imwrite(
            str(out_path), out_frame, [cv2.IMWRITE_JPEG_QUALITY, cv2_q]
        )

    while True:
        if cancel_event.is_set():
            break
        ok, frame = cap.read()
        if not ok:
            break
        src_idx += 1

        if last_features is None:
            # Always keep the first frame (no previous to compare to).
            dst_idx = start_number + written
            _save(frame, dst_idx)
            src_indices_kept.append(src_idx)
            written += 1
            last_kept_src = src_idx
            last_features = estimator.features(frame)
            log(f"    KEEP src #{src_idx} -> {dst_idx}.jpg "
                f"(overlap=N/A, first frame)")
        else:
            fcur = estimator.features(frame)
            ov = estimator.overlap(last_features, fcur)
            keep = False
            reason = ""
            if ov <= 0.0:
                # Match failed entirely -> probably a big jump or texture
                # loss. Re-anchor on this frame.
                if src_idx - last_kept_src >= 1:
                    keep = True
                    reason = "match-fail re-anchor"
            elif ov <= upper:
                keep = True
                reason = f"<= upper {upper*100:.0f}%"
            # else: overlap still too high -> skip and keep walking.
            last_overlap = ov

            if keep:
                dst_idx = start_number + written
                _save(frame, dst_idx)
                src_indices_kept.append(src_idx)
                written += 1
                gap = src_idx - last_kept_src
                last_kept_src = src_idx
                last_features = fcur
                log(f"    KEEP src #{src_idx} -> {dst_idx}.jpg "
                    f"(overlap={ov*100:.1f}%, gap={gap} frames, {reason})")

        # Progress: based on source-frame index walked, not output frames.
        set_progress(base_progress + src_idx, total_progress)

        if src_idx % 200 == 0:
            log(f"    ...walked src {src_idx}/{nframes_cv}, "
                f"kept {written}, last overlap={last_overlap*100:.1f}%")

    cap.release()

    if cancel_event.is_set():
        log("  Cancelled.")
        return 0

    log(f"  Wrote {written} JPEGs (kept src indices: "
        f"{src_indices_kept[0] if src_indices_kept else '-'}..."
        f"{src_indices_kept[-1] if src_indices_kept else '-'}).")
    set_progress(base_progress + nframes_cv, total_progress)

    _write_gps_exif(output_dir, start_number, written, src_indices_kept,
                    gps_map, video.stem, log)
    return written


# --- GPS EXIF (shared by both modes) ----------------------------------------

def _write_gps_exif(output_dir: Path, start_number: int, written: int,
                    src_indices, gps_map, stem: str, log):
    if written <= 0:
        return
    csv_path = output_dir / f".exif_batch_{stem}.csv"
    rows = 0
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "SourceFile",
            "GPSLatitude", "GPSLatitudeRef",
            "GPSLongitude", "GPSLongitudeRef",
            "GPSAltitude", "GPSAltitudeRef",
        ])
        for i in range(written):
            if i >= len(src_indices):
                break
            src_idx = src_indices[i]
            gps = gps_map.get(src_idx)
            if gps is None:
                continue
            lat, lon, alt = gps
            jpg = output_dir / f"{start_number + i}.jpg"
            w.writerow([
                str(jpg.resolve()),
                abs(lat), "N" if lat >= 0 else "S",
                abs(lon), "E" if lon >= 0 else "W",
                abs(alt), 0 if alt >= 0 else 1,
            ])
            rows += 1

    if rows > 0:
        log(f"  Writing GPS EXIF to {rows} files via exiftool...")
        r = subprocess.run(
            ["exiftool", f"-csv={csv_path}", "-overwrite_original",
             "-q", "-q", str(output_dir)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            log(f"  exiftool stderr: {r.stderr.strip()}")
        else:
            log("  EXIF done.")
    else:
        log("  No GPS data available; JPEGs written without EXIF.")
    try:
        csv_path.unlink()
    except OSError:
        pass


# --- Top-level driver -------------------------------------------------------

def process_all(
    input_dir: Path,
    output_dir: Path,
    mode: str,                # "fixed" or "adaptive"
    interval: int,
    resolution,
    jpeg_quality: int,
    hwaccel,                  # for fixed mode
    target_overlap: float,    # for adaptive mode
    tolerance: float,
    detector_name: str,       # "ORB" or "SIFT"
    prefer_cv2_cuda: bool,
    feature_long_side: int,
    log,
    set_progress,
    cancel_event: threading.Event,
):
    videos = sorted(
        [p for p in input_dir.iterdir()
         if p.is_file() and p.suffix.lower() == ".mp4"
         and not p.name.startswith("._")],
        key=lambda p: p.name,
    )
    if not videos:
        log(f"No MP4 files found in {input_dir}")
        return

    log(f"Mode: {mode}")
    if mode == "fixed":
        if hwaccel:
            log(f"GPU decode: ON -> {hwaccel['name']} ({hwaccel['description']})")
        else:
            log("GPU decode: OFF (CPU)")
    else:
        log(f"Detector: {detector_name}, "
            f"target overlap={target_overlap*100:.0f}% "
            f"(+/-{tolerance*100:.0f}%), "
            f"prefer cv2.cuda={prefer_cv2_cuda}")

    log(f"Found {len(videos)} video(s). Planning...")

    plans = []
    total_units = 0  # progress unit = source frame walked
    for v in videos:
        srt_path, gps_map, max_src, _n_expected = plan_video(v, interval)
        plans.append((v, srt_path, gps_map, max_src))
        # In fixed mode, ffmpeg's frame= counter goes up to ~ n_expected.
        # In adaptive mode, we walk every src frame.
        if mode == "fixed":
            n_expected = (max_src + interval - 1) // interval if max_src > 0 else 0
            total_units += n_expected
        else:
            total_units += max_src
        log(f"  - {v.name}: {max_src} src frames "
            f"(SRT: {'yes' if srt_path else 'MISSING'})")
    log(f"Total progress units: {total_units}")
    log("")

    if total_units == 0:
        log("Nothing to extract.")
        return

    set_progress(0, total_units)
    output_dir.mkdir(parents=True, exist_ok=True)

    counter = 1
    base = 0
    total_written = 0
    for i, (video, srt_path, gps_map, max_src) in enumerate(plans):
        if cancel_event.is_set():
            log("Cancelled.")
            break
        log(f"[{i + 1}/{len(plans)}] {video.name}")
        if mode == "fixed":
            written = extract_video_fixed(
                video, srt_path, gps_map, max_src,
                output_dir, counter, interval, resolution, jpeg_quality,
                hwaccel,
                base, total_units,
                log, set_progress, cancel_event,
            )
            base += (max_src + interval - 1) // interval if max_src > 0 else 0
        else:
            written = extract_video_adaptive(
                video, srt_path, gps_map, max_src,
                output_dir, counter,
                target_overlap, tolerance,
                detector_name, prefer_cv2_cuda, feature_long_side,
                resolution, jpeg_quality,
                base, total_units,
                log, set_progress, cancel_event,
            )
            base += max_src
        counter += written
        total_written += written
        set_progress(base, total_units)

    log("")
    log(f"=== Done. Extracted {total_written} frames into {output_dir} ===")


# --- GUI --------------------------------------------------------------------

RES_PRESETS = [
    "Original", "3840x2160", "2560x1440", "1920x1080",
    "1280x720", "960x540", "Custom...",
]

MODES = ["Fixed interval", "Adaptive (similarity)"]
DETECTORS = ["ORB (fast, GPU-capable)", "SIFT (robust, CPU only)"]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DJI Frame Extractor -- GPS EXIF + Similarity")
        self.geometry("880x780")

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.mode_var = tk.StringVar(value=MODES[0])

        # Fixed mode
        self.interval_var = tk.IntVar(value=1)

        # Adaptive mode
        self.detector_var = tk.StringVar(value=DETECTORS[0])
        self.target_overlap_var = tk.IntVar(value=30)   # %
        self.tolerance_var = tk.IntVar(value=5)         # %
        self.feature_side_var = tk.IntVar(value=720)    # px
        self.use_cv2_cuda_var = tk.BooleanVar(value=False)
        self.cv2_cuda_status_var = tk.StringVar(value="(not probed)")

        # Common
        self.quality_var = tk.IntVar(value=2)
        self.res_preset_var = tk.StringVar(value="Original")
        self.custom_res_var = tk.StringVar(value="1920x1080")
        self.use_gpu_var = tk.BooleanVar(value=False)
        self.gpu_status_var = tk.StringVar(value="(not probed)")
        self.progress_label_var = tk.StringVar(value="Idle.")

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None

        self.detected_hwaccel = probe_hwaccel()
        self.cv2_cuda_ok, self.cv2_cuda_info = probe_cv2_cuda()

        self._build_ui()
        self._update_custom_entry_state()
        self._update_gpu_status_label()
        self._update_cv2_cuda_label()
        self._update_mode_visibility()
        self.after(100, self._flush_log)

    # ------------------------------------------------------------ UI build
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        paths = ttk.Frame(self)
        paths.pack(fill="x", **pad)
        paths.columnconfigure(1, weight=1)
        ttk.Label(paths, text="Input folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(paths, textvariable=self.input_var).grid(
            row=0, column=1, sticky="we", padx=4)
        ttk.Button(paths, text="Browse...", command=self._pick_in).grid(
            row=0, column=2)
        ttk.Label(paths, text="Output folder:").grid(row=1, column=0, sticky="w")
        ttk.Entry(paths, textvariable=self.output_var).grid(
            row=1, column=1, sticky="we", padx=4)
        ttk.Button(paths, text="Browse...", command=self._pick_out).grid(
            row=1, column=2)

        # ---- Mode selector ----
        mode_frame = ttk.LabelFrame(self, text="Extraction mode")
        mode_frame.pack(fill="x", **pad)
        self.mode_combo = ttk.Combobox(
            mode_frame, textvariable=self.mode_var, values=MODES,
            state="readonly", width=24,
        )
        self.mode_combo.pack(side="left", padx=4, pady=4)
        self.mode_combo.bind(
            "<<ComboboxSelected>>", lambda _e: self._update_mode_visibility(),
        )
        ttk.Label(
            mode_frame,
            text="(Adaptive keeps frames so consecutive ones overlap by a target %)",
            foreground="#555",
        ).pack(side="left", padx=8)

        # ---- Fixed-mode options ----
        self.fixed_frame = ttk.LabelFrame(self, text="Fixed-interval options")
        self.fixed_frame.pack(fill="x", **pad)
        ttk.Label(self.fixed_frame,
                  text="Frame interval (1 = every frame):").grid(
            row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Spinbox(self.fixed_frame, from_=1, to=600,
                    textvariable=self.interval_var, width=6).grid(
            row=0, column=1, sticky="w")

        # ---- Adaptive-mode options ----
        self.adaptive_frame = ttk.LabelFrame(self, text="Adaptive (similarity) options")
        self.adaptive_frame.pack(fill="x", **pad)

        ttk.Label(self.adaptive_frame, text="Detector:").grid(
            row=0, column=0, sticky="w", padx=4, pady=4)
        det_combo = ttk.Combobox(
            self.adaptive_frame, textvariable=self.detector_var,
            values=DETECTORS, state="readonly", width=28,
        )
        det_combo.grid(row=0, column=1, sticky="w")
        det_combo.bind(
            "<<ComboboxSelected>>",
            lambda _e: self._update_cv2_cuda_label(),
        )

        ttk.Label(self.adaptive_frame, text="Target overlap (%):").grid(
            row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Spinbox(self.adaptive_frame, from_=10, to=95,
                    textvariable=self.target_overlap_var, width=6).grid(
            row=1, column=1, sticky="w")
        ttk.Label(self.adaptive_frame,
                  text="(25-35% = aggressive thinning; 60-75% = COLMAP-friendly)",
                  foreground="#555").grid(
            row=1, column=2, sticky="w", padx=8)

        ttk.Label(self.adaptive_frame, text="Tolerance (+/- %):").grid(
            row=2, column=0, sticky="w", padx=4, pady=4)
        ttk.Spinbox(self.adaptive_frame, from_=1, to=20,
                    textvariable=self.tolerance_var, width=6).grid(
            row=2, column=1, sticky="w")

        ttk.Label(self.adaptive_frame,
                  text="Feature long side (px):").grid(
            row=3, column=0, sticky="w", padx=4, pady=4)
        ttk.Spinbox(self.adaptive_frame, from_=240, to=1920, increment=60,
                    textvariable=self.feature_side_var, width=6).grid(
            row=3, column=1, sticky="w")
        ttk.Label(self.adaptive_frame,
                  text="(downsample for feature detection -- output frames are full res)",
                  foreground="#555").grid(
            row=3, column=2, sticky="w", padx=8)

        ttk.Label(self.adaptive_frame, text="cv2.cuda for ORB:").grid(
            row=4, column=0, sticky="w", padx=4, pady=4)
        cuda_row = ttk.Frame(self.adaptive_frame)
        cuda_row.grid(row=4, column=1, columnspan=2, sticky="w")
        ttk.Checkbutton(cuda_row, text="Use cv2.cuda (auto-detect)",
                        variable=self.use_cv2_cuda_var,
                        command=self._update_cv2_cuda_label).pack(side="left")
        ttk.Label(cuda_row, textvariable=self.cv2_cuda_status_var,
                  foreground="#555").pack(side="left", padx=(8, 0))
        ttk.Button(cuda_row, text="Re-detect",
                   command=self._redetect_cv2_cuda).pack(
            side="left", padx=(8, 0))

        # ---- Common options ----
        opts = ttk.LabelFrame(self, text="Output options (apply to both modes)")
        opts.pack(fill="x", **pad)
        self._opts_anchor = opts  # used for re-packing fixed/adaptive frames

        ttk.Label(opts, text="Output resolution:").grid(
            row=0, column=0, sticky="w", padx=4, pady=4)
        res_row = ttk.Frame(opts)
        res_row.grid(row=0, column=1, sticky="w")
        self.res_combo = ttk.Combobox(
            res_row, textvariable=self.res_preset_var, values=RES_PRESETS,
            state="readonly", width=12,
        )
        self.res_combo.pack(side="left")
        self.res_combo.bind(
            "<<ComboboxSelected>>",
            lambda _e: self._update_custom_entry_state(),
        )
        ttk.Label(res_row, text="  Custom (WxH):").pack(side="left")
        self.custom_entry = ttk.Entry(
            res_row, textvariable=self.custom_res_var, width=12,
        )
        self.custom_entry.pack(side="left")

        ttk.Label(opts, text="JPEG quality (1=best, 31=worst):").grid(
            row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Spinbox(opts, from_=1, to=31, textvariable=self.quality_var,
                    width=6).grid(row=1, column=1, sticky="w")

        ttk.Label(opts, text="GPU decode (Fixed mode only):").grid(
            row=2, column=0, sticky="w", padx=4, pady=4)
        gpu_row = ttk.Frame(opts)
        gpu_row.grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(gpu_row, text="Use GPU (auto-detect)",
                        variable=self.use_gpu_var,
                        command=self._update_gpu_status_label).pack(side="left")
        ttk.Label(gpu_row, textvariable=self.gpu_status_var,
                  foreground="#555").pack(side="left", padx=(8, 0))
        ttk.Button(gpu_row, text="Re-detect",
                   command=self._redetect_gpu).pack(side="left", padx=(8, 0))

        # ---- Run controls ----
        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="Start", command=self._start)
        self.start_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self._cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=4)

        prog_frame = ttk.Frame(self)
        prog_frame.pack(fill="x", padx=8, pady=4)
        self.progress = ttk.Progressbar(prog_frame, mode="determinate")
        self.progress.pack(fill="x", side="top")
        ttk.Label(prog_frame, textvariable=self.progress_label_var,
                  anchor="w").pack(fill="x", side="top")

        log_frame = ttk.Frame(self)
        log_frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.log = tk.Text(log_frame, wrap="word", height=18)
        sb = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    # --------------------------------------------------------- UI helpers
    def _update_mode_visibility(self):
        anchor = getattr(self, "_opts_anchor", None)
        if self.mode_var.get() == MODES[0]:
            self.adaptive_frame.pack_forget()
            self.fixed_frame.pack_forget()
            if anchor is not None:
                self.fixed_frame.pack(fill="x", padx=8, pady=4, before=anchor)
            else:
                self.fixed_frame.pack(fill="x", padx=8, pady=4)
        else:
            self.fixed_frame.pack_forget()
            self.adaptive_frame.pack_forget()
            if anchor is not None:
                self.adaptive_frame.pack(fill="x", padx=8, pady=4, before=anchor)
            else:
                self.adaptive_frame.pack(fill="x", padx=8, pady=4)

    def _update_custom_entry_state(self):
        state = "normal" if self.res_preset_var.get() == "Custom..." else "disabled"
        self.custom_entry.configure(state=state)

    def _update_gpu_status_label(self):
        if self.detected_hwaccel is None:
            label = f"{platform.system()}: no GPU hwaccel detected"
        else:
            label = (f"{platform.system()}: "
                     f"{self.detected_hwaccel['name']} "
                     f"({self.detected_hwaccel['description']})")
        if self.use_gpu_var.get() and self.detected_hwaccel is None:
            label += "  -- will fall back to CPU"
        self.gpu_status_var.set(label)

    def _redetect_gpu(self):
        self.detected_hwaccel = probe_hwaccel()
        self._update_gpu_status_label()
        self._log(f">> Re-detected ffmpeg GPU: {self.gpu_status_var.get()}")

    def _update_cv2_cuda_label(self):
        det = self.detector_var.get()
        is_orb = det.startswith("ORB")
        if not is_orb:
            self.cv2_cuda_status_var.set("SIFT is CPU-only (cv2 has no GPU SIFT)")
            return
        msg = self.cv2_cuda_info
        if self.use_cv2_cuda_var.get() and not self.cv2_cuda_ok:
            msg += "  -- will fall back to CPU ORB"
        self.cv2_cuda_status_var.set(msg)

    def _redetect_cv2_cuda(self):
        self.cv2_cuda_ok, self.cv2_cuda_info = probe_cv2_cuda()
        self._update_cv2_cuda_label()
        self._log(f">> Re-detected cv2.cuda: {self.cv2_cuda_info}")

    def _pick_in(self):
        d = filedialog.askdirectory(title="Select input folder (MP4 + SRT)")
        if d:
            self.input_var.set(d)
            if not self.output_var.get():
                self.output_var.set(str(Path(d) / "frames"))

    def _pick_out(self):
        d = filedialog.askdirectory(title="Select output folder for JPEGs")
        if d:
            self.output_var.set(d)

    def _log(self, msg: str):
        self.log_queue.put(msg)

    def _flush_log(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log.insert("end", msg + "\n")
                self.log.see("end")
        except queue.Empty:
            pass
        self.after(100, self._flush_log)

    def _set_progress(self, current: int, total: int):
        def apply():
            self.progress["maximum"] = max(1, total)
            self.progress["value"] = min(current, total)
            if total > 0:
                pct = 100.0 * current / total
                self.progress_label_var.set(
                    f"Progress: {current} / {total} ({pct:.1f}%)"
                )
            else:
                self.progress_label_var.set("Progress: 0 / 0")
        self.after(0, apply)

    def _resolve_resolution(self):
        sel = self.res_preset_var.get()
        if sel == "Original":
            return None
        if sel == "Custom...":
            res = parse_resolution(self.custom_res_var.get())
            if res is None:
                raise ValueError(
                    f"Invalid custom resolution: {self.custom_res_var.get()!r}. "
                    "Use format like 1920x1080."
                )
            return res
        return parse_resolution(sel)

    # ---------------------------------------------------------- run/cancel
    def _start(self):
        in_dir = self.input_var.get().strip()
        out_dir = self.output_var.get().strip()
        if not in_dir or not out_dir:
            messagebox.showerror("Error", "Please pick input and output folders.")
            return
        in_path = Path(in_dir)
        out_path = Path(out_dir)
        if not in_path.is_dir():
            messagebox.showerror("Error", f"Input is not a directory:\n{in_dir}")
            return
        try:
            resolution = self._resolve_resolution()
        except ValueError as e:
            messagebox.showerror("Invalid resolution", str(e))
            return

        for tool in ("ffmpeg", "ffprobe", "exiftool"):
            if shutil.which(tool) is None:
                messagebox.showerror(
                    "Missing tool",
                    f"Required tool not found in PATH: {tool}\n\n"
                    "macOS/Linux: brew/apt install ffmpeg exiftool\n"
                    "Windows: install from their websites and add to PATH.",
                )
                return

        is_adaptive = self.mode_var.get() == MODES[1]
        if is_adaptive:
            cv2, np, err = _try_import_cv2()
            if cv2 is None:
                messagebox.showerror(
                    "Missing dependency",
                    "Adaptive mode requires opencv-python.\n\n"
                    "Install with:\n    pip install opencv-python numpy\n\n"
                    f"(import error: {err})",
                )
                return
            mode = "adaptive"
            interval = 1
            target = max(0.05, min(0.95, self.target_overlap_var.get() / 100.0))
            tol = max(0.01, min(0.30, self.tolerance_var.get() / 100.0))
            detector = "ORB" if self.detector_var.get().startswith("ORB") else "SIFT"
            prefer_cuda = bool(self.use_cv2_cuda_var.get()) and detector == "ORB"
            feature_side = max(240, min(1920, self.feature_side_var.get()))
            hwaccel = None
        else:
            mode = "fixed"
            interval = max(1, self.interval_var.get())
            target = 0.0
            tol = 0.0
            detector = "ORB"
            prefer_cuda = False
            feature_side = 720
            hwaccel = self.detected_hwaccel if self.use_gpu_var.get() else None
            if self.use_gpu_var.get() and hwaccel is None:
                if not messagebox.askyesno(
                    "No GPU hwaccel detected",
                    "GPU acceleration is enabled but no supported hwaccel was "
                    "detected on this machine.\n\nContinue with CPU decoding?",
                ):
                    return

        if out_path.exists() and any(out_path.iterdir()):
            if not messagebox.askyesno(
                "Output not empty",
                f"Output folder is not empty:\n{out_path}\n\n"
                "Files with the same name (1.jpg, 2.jpg, ...) will be "
                "overwritten.\nContinue?",
            ):
                return

        self.cancel_event.clear()
        self.start_btn["state"] = "disabled"
        self.cancel_btn["state"] = "normal"
        self.log.delete("1.0", "end")
        self.progress["value"] = 0
        self.progress_label_var.set("Starting...")

        quality = max(1, min(31, self.quality_var.get()))

        self.worker = threading.Thread(
            target=self._run, daemon=True,
            args=(
                in_path, out_path, mode, interval, resolution, quality,
                hwaccel, target, tol, detector, prefer_cuda, feature_side,
            ),
        )
        self.worker.start()

    def _cancel(self):
        self.cancel_event.set()
        self._log(">> Cancel requested.")

    def _run(self, in_dir, out_dir, mode, interval, resolution, quality,
             hwaccel, target, tol, detector, prefer_cuda, feature_side):
        try:
            process_all(
                in_dir, out_dir, mode, interval, resolution, quality,
                hwaccel, target, tol, detector, prefer_cuda, feature_side,
                self._log, self._set_progress, self.cancel_event,
            )
        except Exception as e:  # noqa: BLE001
            self._log(f"ERROR: {e}")
        finally:
            self.after(0, self._done)

    def _done(self):
        self.start_btn["state"] = "normal"
        self.cancel_btn["state"] = "disabled"


def main():
    # --cli  fixed       INPUT OUTPUT [INTERVAL] [WxH|-] [JPEG_Q] [--gpu]
    # --cli  adaptive    INPUT OUTPUT [TARGET%] [TOL%] [ORB|SIFT] [WxH|-]
    #                    [JPEG_Q] [--cv2-cuda] [--feat-side N]
    if "--cli" in sys.argv:
        args = [a for a in sys.argv[1:] if a != "--cli"]
        use_gpu = "--gpu" in args
        prefer_cuda = "--cv2-cuda" in args
        feat_side = 720
        if "--feat-side" in args:
            i = args.index("--feat-side")
            feat_side = int(args[i + 1])
            del args[i:i + 2]
        args = [a for a in args
                if a not in ("--gpu", "--cv2-cuda")]

        if not args:
            print("Usage:\n"
                  "  --cli fixed    INPUT OUTPUT [INTERVAL] [WxH|-] [JPEG_Q] "
                  "[--gpu]\n"
                  "  --cli adaptive INPUT OUTPUT [TARGET%] [TOL%] "
                  "[ORB|SIFT] [WxH|-] [JPEG_Q] [--cv2-cuda] [--feat-side N]")
            sys.exit(2)

        sub = args[0].lower()
        rest = args[1:]
        cancel = threading.Event()

        def cli_progress(cur, total):
            if total > 0:
                sys.stdout.write(
                    f"\rProgress: {cur}/{total} ({100.0*cur/total:5.1f}%)   "
                )
                sys.stdout.flush()

        if sub == "fixed":
            if len(rest) < 2:
                print("Need INPUT OUTPUT for fixed mode.")
                sys.exit(2)
            in_dir = Path(rest[0]); out_dir = Path(rest[1])
            interval = int(rest[2]) if len(rest) > 2 else 1
            res_arg = rest[3] if len(rest) > 3 else "-"
            quality = int(rest[4]) if len(rest) > 4 else 2
            resolution = (None if res_arg in ("-", "", "original")
                          else parse_resolution(res_arg))
            hwaccel = probe_hwaccel() if use_gpu else None
            try:
                process_all(
                    in_dir, out_dir, "fixed", interval, resolution, quality,
                    hwaccel, 0.0, 0.0, "ORB", False, 720,
                    lambda m: print(m, flush=True), cli_progress, cancel,
                )
            finally:
                print()
            return

        if sub == "adaptive":
            if len(rest) < 2:
                print("Need INPUT OUTPUT for adaptive mode.")
                sys.exit(2)
            in_dir = Path(rest[0]); out_dir = Path(rest[1])
            target_pct = float(rest[2]) if len(rest) > 2 else 30.0
            tol_pct = float(rest[3]) if len(rest) > 3 else 5.0
            detector = (rest[4].upper() if len(rest) > 4 else "ORB")
            res_arg = rest[5] if len(rest) > 5 else "-"
            quality = int(rest[6]) if len(rest) > 6 else 2
            resolution = (None if res_arg in ("-", "", "original")
                          else parse_resolution(res_arg))
            try:
                process_all(
                    in_dir, out_dir, "adaptive", 1, resolution, quality,
                    None, target_pct / 100.0, tol_pct / 100.0,
                    detector, prefer_cuda, feat_side,
                    lambda m: print(m, flush=True), cli_progress, cancel,
                )
            finally:
                print()
            return

        print(f"Unknown sub-command: {sub}")
        sys.exit(2)

    App().mainloop()


if __name__ == "__main__":
    main()
