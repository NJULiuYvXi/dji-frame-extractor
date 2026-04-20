#!/usr/bin/env python3
"""
DJI drone video -> sequential JPEG frames with GPS EXIF metadata.

GUI lets you pick an input folder (containing *.MP4 + matching *.SRT from the
DJI drone) and an output folder. All videos in the input folder are processed
in filename order; frames are written to the single output folder with
globally-sequential names 1.jpg, 2.jpg, 3.jpg, ... and each frame gets GPS
EXIF tags from the matching SRT entry.

Options:
  - Frame interval (1 = every frame, N = every Nth frame).
  - Output resolution (Original, 3840x2160, 1920x1080, ..., or Custom).
  - JPEG quality (1 best -> 31 worst; ffmpeg -q:v).

External dependencies (must be in PATH):
  - ffmpeg / ffprobe
  - exiftool

Only stdlib is used on the Python side.
"""

import csv
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

# One SRT block contains "SrtCnt : N" and, on a later line, the [latitude:]
# [longitude:] [altitude:] triple. DOTALL so ".*?" can cross newlines.
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
    """Find matching .SRT next to the video, case-insensitive on extension."""
    for ext in (".SRT", ".srt", ".Srt"):
        p = video_path.with_suffix(ext)
        if p.exists():
            return p
    return None


def probe_frame_count(video_path: Path) -> int:
    """Fallback frame-count via ffprobe (counts packets, close enough)."""
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
    """Parse '3840x2160' -> (3840, 2160). Return None for falsy/invalid."""
    if not s:
        return None
    m = RES_RE.match(s)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


# --- Core processing --------------------------------------------------------

def plan_video(video: Path, interval: int):
    """Return (srt_path, gps_map, max_src, n_expected)."""
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
    base_progress: int,
    total_progress: int,
    log,
    set_progress,
    cancel_event: threading.Event,
):
    """
    Extract frames from one video. Returns frames_written.
    The i-th extracted JPEG (1-indexed within this video) maps to source frame
    1 + (i-1)*interval, so its filename is `start_number + i - 1`.
    Progress is reported incrementally via `set_progress(base + n, total)`
    where n is the running frame count within this video.
    """
    log(f"  SRT: {srt_path.name if srt_path else 'MISSING'} "
        f"({len(gps_map)} GPS entries)")
    if max_src <= 0:
        log("  Could not determine frame count; skipping.")
        return 0

    selected_src = list(range(1, max_src + 1, interval))
    if not selected_src:
        return 0

    # Build -vf filter chain. `n` in select is 0-indexed.
    vf_parts = []
    if interval > 1:
        vf_parts.append(f"select='not(mod(n\\,{interval}))'")
    if resolution is not None:
        vf_parts.append(f"scale={resolution[0]}:{resolution[1]}:flags=lanczos")

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-i", str(video),
    ]
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
    if interval > 1:
        # select drops frames -> use VFR so ffmpeg doesn't duplicate.
        cmd += ["-fps_mode", "vfr"]
    cmd += [
        "-q:v", str(jpeg_quality),
        "-start_number", str(start_number),
        "-progress", "pipe:1",
        "-nostats",
        str(output_dir / "%d.jpg"),
    ]

    res_label = f"{resolution[0]}x{resolution[1]}" if resolution else "original"
    log(f"  Extracting ~{len(selected_src)} frames "
        f"(res: {res_label}, q={jpeg_quality}) with ffmpeg...")

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    # Read stderr (warnings/errors) in a background thread so it never blocks.
    def pump_stderr():
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                log(f"    ffmpeg: {line}")

    t_err = threading.Thread(target=pump_stderr, daemon=True)
    t_err.start()

    # Read stdout for -progress key=value lines. `frame=N` arrives periodically.
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
        raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")

    # Count the JPEGs actually produced for this video.
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

    # Write GPS EXIF via a CSV batch to exiftool.
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
                # No GPS for this frame; leave JPEG without EXIF.
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

    log(f"Found {len(videos)} video(s). Planning...")

    # Pre-plan each video so we know the total expected frame count for the
    # progress bar before we start extracting.
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
            base, total_expected,
            log, set_progress, cancel_event,
        )
        counter += written
        base += written
        total_written += written
        # Snap progress to the accumulated written count so later videos
        # don't inherit a stale "ahead of actual" position.
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
        self.title("DJI Frame Extractor with GPS EXIF")
        self.geometry("780x640")

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.interval_var = tk.IntVar(value=1)
        self.quality_var = tk.IntVar(value=2)
        self.res_preset_var = tk.StringVar(value="Original")
        self.custom_res_var = tk.StringVar(value="1920x1080")
        self.progress_label_var = tk.StringVar(value="Idle.")

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None

        self._build_ui()
        self._update_custom_entry_state()
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
        # Called from worker thread; marshal to Tk main thread.
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
                    "Install via Homebrew:\n"
                    "  brew install ffmpeg exiftool",
                )
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
            args=(in_path, out_path, interval, resolution, quality),
        )
        self.worker.start()

    def _cancel(self):
        self.cancel_event.set()
        self._log(">> Cancel requested (will stop after current ffmpeg call).")

    def _run(self, in_dir: Path, out_dir: Path, interval: int,
             resolution, quality: int):
        try:
            process_all(
                in_dir, out_dir, interval, resolution, quality,
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
    # CLI mode: --cli INPUT OUTPUT [INTERVAL] [WxH|-] [JPEG_Q]
    if "--cli" in sys.argv:
        args = [a for a in sys.argv[1:] if a != "--cli"]
        if len(args) < 2:
            print("Usage: extract_frames_with_gps.py --cli INPUT OUTPUT "
                  "[INTERVAL] [WxH|-] [JPEG_Q]")
            sys.exit(2)
        in_dir = Path(args[0])
        out_dir = Path(args[1])
        interval = int(args[2]) if len(args) > 2 else 1
        res_arg = args[3] if len(args) > 3 else "-"
        quality = int(args[4]) if len(args) > 4 else 2
        resolution = None if res_arg in ("-", "", "original") else parse_resolution(res_arg)
        if res_arg not in ("-", "", "original") and resolution is None:
            print(f"Invalid resolution: {res_arg}")
            sys.exit(2)
        cancel = threading.Event()

        def cli_progress(cur, total):
            if total > 0:
                sys.stdout.write(
                    f"\rProgress: {cur}/{total} ({100.0*cur/total:5.1f}%)   "
                )
                sys.stdout.flush()

        try:
            process_all(
                in_dir, out_dir, interval, resolution, quality,
                lambda m: print(m, flush=True),
                cli_progress, cancel,
            )
        finally:
            print()
        return

    App().mainloop()


if __name__ == "__main__":
    main()
