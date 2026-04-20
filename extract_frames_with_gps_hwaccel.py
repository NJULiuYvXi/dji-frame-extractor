#!/usr/bin/env python3
"""
DJI drone video -> sequential JPEG frames with GPS EXIF metadata.

This is the hwaccel-enabled variant of extract_frames_with_gps.py. It adds a
"Use GPU acceleration (auto-detect)" checkbox (default OFF). When enabled,
the script probes the OS + ffmpeg build + actual GPU hardware and picks an
appropriate hwaccel for H.264/HEVC decoding:

  macOS  -> videotoolbox
  Linux  -> cuda (if NVIDIA GPU present) else vaapi (if /dev/dri/renderD*)
           else vdpau
  Windows-> cuda (if NVIDIA GPU present) else d3d11va / qsv / dxva2

GPU is used for decode only; Lanczos scaling and JPEG encoding stay on CPU
(no cross-platform GPU JPEG encoder exists). If the requested hwaccel isn't
usable at runtime, ffmpeg will error out -- uncheck the box and try again.

Everything else (frame interval, output resolution, JPEG quality, frame-level
progress bar, GPS EXIF via exiftool) is the same as extract_frames_with_gps.py.

External dependencies (must be in PATH):
  - ffmpeg / ffprobe
  - exiftool

Only stdlib is used on the Python side.
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


# --- SRT parsing ------------------------------------------------------------

SRT_BLOCK_RE = re.compile(
    r"SrtCnt\s*:\s*(\d+)"
    r".*?\[\s*latitude\s*:\s*([-\d.]+)\s*\]"
    r"\s*\[\s*longitude\s*:\s*([-\d.]+)\s*\]"
    r"\s*\[\s*altitude\s*:\s*([-\d.]+)\s*\]",
    re.DOTALL,
)

RES_RE = re.compile(r"^\s*(\d+)\s*[x×*]\s*(\d+)\s*$", re.IGNORECASE)


def parse_srt(srt_path: Path):
    """Return dict: source_frame_index (1-based) -> (lat, lon, alt)."""
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
    except (subprocess.CalledProcessError, ValueError):
        return 0


def parse_resolution(s: str):
    if not s:
        return None
    m = RES_RE.match(s)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


# --- Hardware acceleration detection ----------------------------------------

def _list_ffmpeg_hwaccels() -> set:
    """Return the set of hwaccels compiled into the local ffmpeg binary."""
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
        # Skip the "Hardware acceleration methods:" header and empty lines.
        if line and not line.endswith(":"):
            out.add(line.lower())
    return out


def _has_nvidia_gpu() -> bool:
    """True if nvidia-smi lists at least one GPU."""
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        r = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and "GPU" in r.stdout


def _has_dri_render_node() -> bool:
    """True if a Linux DRM render node exists (AMD/Intel via VAAPI)."""
    for i in range(128, 192):
        if os.path.exists(f"/dev/dri/renderD{i}"):
            return True
    return False


def _vaapi_device_path() -> str:
    """Pick the first render node; fall back to the default."""
    for i in range(128, 192):
        p = f"/dev/dri/renderD{i}"
        if os.path.exists(p):
            return p
    return "/dev/dri/renderD128"


def probe_hwaccel():
    """
    Auto-detect the best available GPU hwaccel for *decoding*.
    Returns dict {"name", "args", "description"} or None.

    Strategy: intersect what ffmpeg supports with what the OS + hardware can
    actually provide. Priority is biased toward per-platform "native" options
    that give the largest speedup in practice.
    """
    available = _list_ffmpeg_hwaccels()
    if not available:
        return None
    system = platform.system()

    # (name_in_ffmpeg, input_args_to_prepend_before_-i, human_description,
    #  availability_predicate)
    if system == "Darwin":
        candidates = [
            ("videotoolbox", ["-hwaccel", "videotoolbox"],
             "Apple VideoToolbox (media engine)",
             lambda: True),
        ]
    elif system == "Linux":
        candidates = [
            ("cuda", ["-hwaccel", "cuda"],
             "NVIDIA CUDA / NVDEC",
             _has_nvidia_gpu),
            ("vaapi",
             ["-hwaccel", "vaapi", "-hwaccel_device", _vaapi_device_path()],
             "VA-API (AMD / Intel on Linux)",
             _has_dri_render_node),
            ("vdpau", ["-hwaccel", "vdpau"],
             "VDPAU (NVIDIA legacy on Linux)",
             _has_nvidia_gpu),
        ]
    elif system == "Windows":
        candidates = [
            ("cuda", ["-hwaccel", "cuda"],
             "NVIDIA CUDA / NVDEC",
             _has_nvidia_gpu),
            ("d3d11va", ["-hwaccel", "d3d11va"],
             "Direct3D 11 Video Acceleration (AMD/Intel/NVIDIA on Windows)",
             lambda: True),
            ("qsv", ["-hwaccel", "qsv"],
             "Intel Quick Sync Video",
             lambda: True),
            ("dxva2", ["-hwaccel", "dxva2"],
             "DirectX Video Acceleration 2 (legacy Windows)",
             lambda: True),
        ]
    else:
        return None

    for name, args, desc, predicate in candidates:
        if name in available and predicate():
            return {"name": name, "args": args, "description": desc}
    return None


# --- Core processing --------------------------------------------------------

def plan_video(video: Path, interval: int):
    srt_path = find_srt(video)
    gps_map = parse_srt(srt_path) if srt_path else {}
    max_src = max(gps_map) if gps_map else probe_frame_count(video)
    n_expected = 0
    if max_src > 0 and interval > 0:
        n_expected = (max_src + interval - 1) // interval
    return srt_path, gps_map, max_src, n_expected


def extract_video(
    video: Path,
    srt_path,
    gps_map: dict,
    max_src: int,
    output_dir: Path,
    start_number: int,
    interval: int,
    resolution,
    jpeg_quality: int,
    hwaccel,          # dict {"name", "args", "description"} or None
    base_progress: int,
    total_progress: int,
    log,
    set_progress,
    cancel_event: threading.Event,
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
    # Input-side options (hwaccel must be before -i).
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
        "-progress", "pipe:1",
        "-nostats",
        str(output_dir / "%d.jpg"),
    ]

    res_label = f"{resolution[0]}x{resolution[1]}" if resolution else "original"
    hw_label = hwaccel["name"] if hwaccel else "cpu"
    log(f"  Extracting ~{len(selected_src)} frames "
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

    last_frame_reported = 0
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("frame="):
                try:
                    n = int(line.split("=", 1)[1])
                except ValueError:
                    n = last_frame_reported
                last_frame_reported = n
                set_progress(base_progress + n, total_progress)
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
            hint = (f"  (Hint: '{hwaccel['name']}' decoding failed. "
                    "Try turning GPU acceleration off and re-running.)")
        raise RuntimeError(
            f"ffmpeg exited with code {proc.returncode}{hint}"
        )

    written = 0
    for i in range(len(selected_src)):
        if (output_dir / f"{start_number + i}.jpg").exists():
            written = i + 1
        else:
            break

    log(f"  Wrote {written} JPEGs.")
    set_progress(base_progress + written, total_progress)
    if written == 0:
        return 0

    csv_path = output_dir / f".exif_batch_{video.stem}.csv"
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
            src_idx = selected_src[i]
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
            [
                "exiftool", f"-csv={csv_path}",
                "-overwrite_original", "-q", "-q",
                str(output_dir),
            ],
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

    return written


def process_all(
    input_dir: Path,
    output_dir: Path,
    interval: int,
    resolution,
    jpeg_quality: int,
    hwaccel,
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

    if hwaccel:
        log(f"GPU acceleration: ON -> {hwaccel['name']} "
            f"({hwaccel['description']})")
    else:
        log("GPU acceleration: OFF (CPU decode)")

    log(f"Found {len(videos)} video(s). Planning...")

    plans = []
    total_expected = 0
    for v in videos:
        srt_path, gps_map, max_src, n_expected = plan_video(v, interval)
        plans.append((v, srt_path, gps_map, max_src, n_expected))
        total_expected += n_expected
        log(f"  - {v.name}: {max_src} src frames -> ~{n_expected} to extract "
            f"(SRT: {'yes' if srt_path else 'MISSING'})")
    log(f"Total expected output frames: {total_expected}")
    log("")

    if total_expected == 0:
        log("Nothing to extract.")
        return

    set_progress(0, total_expected)
    output_dir.mkdir(parents=True, exist_ok=True)

    counter = 1
    base = 0
    total_written = 0
    for i, (video, srt_path, gps_map, max_src, n_expected) in enumerate(plans):
        if cancel_event.is_set():
            log("Cancelled.")
            break
        log(f"[{i + 1}/{len(plans)}] {video.name}")
        written = extract_video(
            video, srt_path, gps_map, max_src,
            output_dir, counter, interval, resolution, jpeg_quality,
            hwaccel,
            base, total_expected,
            log, set_progress, cancel_event,
        )
        counter += written
        base += written
        total_written += written
        set_progress(base, total_expected)

    log("")
    log(f"=== Done. Extracted {total_written} frames into {output_dir} ===")


# --- GUI --------------------------------------------------------------------

RES_PRESETS = [
    "Original",
    "3840x2160",
    "2560x1440",
    "1920x1080",
    "1280x720",
    "960x540",
    "Custom...",
]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DJI Frame Extractor with GPS EXIF (hwaccel)")
        self.geometry("820x680")

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.interval_var = tk.IntVar(value=1)
        self.quality_var = tk.IntVar(value=2)
        self.res_preset_var = tk.StringVar(value="Original")
        self.custom_res_var = tk.StringVar(value="1920x1080")
        self.use_gpu_var = tk.BooleanVar(value=False)
        self.gpu_status_var = tk.StringVar(value="(not probed)")
        self.progress_label_var = tk.StringVar(value="Idle.")

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None

        # Probe once at startup so the label shows what's available.
        self.detected_hwaccel = probe_hwaccel()

        self._build_ui()
        self._update_custom_entry_state()
        self._update_gpu_status_label()
        self.after(100, self._flush_log)

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

        opts = ttk.LabelFrame(self, text="Options")
        opts.pack(fill="x", **pad)

        ttk.Label(opts, text="Frame interval (1 = every frame):").grid(
            row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Spinbox(opts, from_=1, to=600, textvariable=self.interval_var,
                    width=6).grid(row=0, column=1, sticky="w")

        ttk.Label(opts, text="Output resolution:").grid(
            row=1, column=0, sticky="w", padx=4, pady=4)
        res_row = ttk.Frame(opts)
        res_row.grid(row=1, column=1, sticky="w")
        self.res_combo = ttk.Combobox(
            res_row, textvariable=self.res_preset_var, values=RES_PRESETS,
            state="readonly", width=12,
        )
        self.res_combo.pack(side="left")
        self.res_combo.bind("<<ComboboxSelected>>",
                            lambda _e: self._update_custom_entry_state())
        ttk.Label(res_row, text="  Custom (WxH):").pack(side="left")
        self.custom_entry = ttk.Entry(
            res_row, textvariable=self.custom_res_var, width=12,
        )
        self.custom_entry.pack(side="left")

        ttk.Label(opts, text="JPEG quality (1=best, 31=worst):").grid(
            row=2, column=0, sticky="w", padx=4, pady=4)
        ttk.Spinbox(opts, from_=1, to=31, textvariable=self.quality_var,
                    width=6).grid(row=2, column=1, sticky="w")

        ttk.Label(opts, text="GPU acceleration:").grid(
            row=3, column=0, sticky="w", padx=4, pady=4)
        gpu_row = ttk.Frame(opts)
        gpu_row.grid(row=3, column=1, sticky="w")
        ttk.Checkbutton(
            gpu_row, text="Use GPU (auto-detect)",
            variable=self.use_gpu_var,
            command=self._update_gpu_status_label,
        ).pack(side="left")
        ttk.Label(gpu_row, textvariable=self.gpu_status_var,
                  foreground="#555").pack(side="left", padx=(8, 0))
        ttk.Button(gpu_row, text="Re-detect",
                   command=self._redetect_gpu).pack(side="left", padx=(8, 0))

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
        self.log = tk.Text(log_frame, wrap="word", height=22)
        sb = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    # ---- GUI callbacks ----

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
        self._log(f">> Re-detected GPU: {self.gpu_status_var.get()}")

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
                    f"Progress: {current} / {total} frames ({pct:.1f}%)"
                )
            else:
                self.progress_label_var.set("Progress: 0 / 0 frames")
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

        # Resolve hwaccel once, here, so the worker sees a stable value.
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
                "Files with the same name (1.jpg, 2.jpg, ...) will be overwritten.\n"
                "Continue?",
            ):
                return

        self.cancel_event.clear()
        self.start_btn["state"] = "disabled"
        self.cancel_btn["state"] = "normal"
        self.log.delete("1.0", "end")
        self.progress["value"] = 0
        self.progress_label_var.set("Starting...")

        interval = max(1, self.interval_var.get())
        quality = max(1, min(31, self.quality_var.get()))

        self.worker = threading.Thread(
            target=self._run, daemon=True,
            args=(in_path, out_path, interval, resolution, quality, hwaccel),
        )
        self.worker.start()

    def _cancel(self):
        self.cancel_event.set()
        self._log(">> Cancel requested (will stop after current ffmpeg call).")

    def _run(self, in_dir, out_dir, interval, resolution, quality, hwaccel):
        try:
            process_all(
                in_dir, out_dir, interval, resolution, quality, hwaccel,
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
    # CLI mode: --cli [--gpu] INPUT OUTPUT [INTERVAL] [WxH|-] [JPEG_Q]
    if "--cli" in sys.argv:
        args = [a for a in sys.argv[1:] if a != "--cli"]
        use_gpu = False
        if "--gpu" in args:
            use_gpu = True
            args = [a for a in args if a != "--gpu"]
        if len(args) < 2:
            print("Usage: extract_frames_with_gps_hwaccel.py --cli [--gpu] "
                  "INPUT OUTPUT [INTERVAL] [WxH|-] [JPEG_Q]")
            sys.exit(2)
        in_dir = Path(args[0])
        out_dir = Path(args[1])
        interval = int(args[2]) if len(args) > 2 else 1
        res_arg = args[3] if len(args) > 3 else "-"
        quality = int(args[4]) if len(args) > 4 else 2
        resolution = (None if res_arg in ("-", "", "original")
                      else parse_resolution(res_arg))
        if res_arg not in ("-", "", "original") and resolution is None:
            print(f"Invalid resolution: {res_arg}")
            sys.exit(2)

        hwaccel = probe_hwaccel() if use_gpu else None
        if use_gpu:
            if hwaccel:
                print(f"GPU: {hwaccel['name']} ({hwaccel['description']})")
            else:
                print("GPU: requested but none detected; falling back to CPU.")

        cancel = threading.Event()

        def cli_progress(cur, total):
            if total > 0:
                sys.stdout.write(
                    f"\rProgress: {cur}/{total} ({100.0*cur/total:5.1f}%)   "
                )
                sys.stdout.flush()

        try:
            process_all(
                in_dir, out_dir, interval, resolution, quality, hwaccel,
                lambda m: print(m, flush=True),
                cli_progress, cancel,
            )
        finally:
            print()
        return

    App().mainloop()


if __name__ == "__main__":
    main()
