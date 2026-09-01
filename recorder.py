"""
Cursor-following screen recorder: captures the framed region and mic audio via ffmpeg.
"""

from __future__ import annotations

import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

RECORDINGS_DIR = Path(__file__).resolve().parent / "recordings"

# Quality: output size comes from aspect ratio; fps is chosen separately.
QUALITY_PRESETS: dict[str, dict] = {
    "low": {
        "key": "low",
        "label": "Low",
        "detail": "720p — smaller file",
        "fps": 30,
        "crf": 28,
        "preset": "veryfast",
        "audio_bitrate": "128k",
        "height_16_9": 720,
    },
    "hd": {
        "key": "hd",
        "label": "HD",
        "detail": "1080p — recommended",
        "fps": 30,
        "crf": 21,
        "preset": "veryfast",
        "audio_bitrate": "192k",
        "height_16_9": 1080,
    },
    "2k": {
        "key": "2k",
        "label": "2K",
        "detail": "1440p — extra detail",
        "fps": 30,
        "crf": 18,
        "preset": "veryfast",
        "audio_bitrate": "192k",
        "height_16_9": 1440,
    },
    "4k": {
        "key": "4k",
        "label": "4K",
        "detail": "2160p — maximum detail (upscaled if the screen is smaller)",
        "fps": 30,
        "crf": 20,
        "preset": "veryfast",
        "audio_bitrate": "192k",
        "height_16_9": 2160,
    },
}

FPS_CHOICES = (24, 30, 60)

_HEIGHT_TO_WIDTH_16_9 = {720: 1280, 1080: 1920, 1440: 2560, 2160: 3840}

NO_AUDIO = "__none__"


def even(n: int) -> int:
    n = int(n)
    return n if n % 2 == 0 else n - 1


def size_for_quality(ratio: str, quality: str) -> tuple[int, int]:
    """Native capture/encode size for a ratio + quality preset."""
    h = int(QUALITY_PRESETS[quality]["height_16_9"])
    w = _HEIGHT_TO_WIDTH_16_9[h]
    if ratio == "16:9":
        return w, h
    if ratio == "9:16":
        return h, w
    raise ValueError(f"Unknown ratio {ratio!r}")


def encoder_preset(quality: str, fps: int) -> str:
    """Pick a realtime x264 preset so 4K / 60 fps can still keep up."""
    if quality in ("4k", "2k") or fps >= 60:
        return "ultrafast"
    return str(QUALITY_PRESETS[quality]["preset"])


def find_ffmpeg() -> str:
    bundled = None
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        bundled = None
    for candidate in (shutil.which("ffmpeg"), bundled):
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise FileNotFoundError(
        "ffmpeg not found. Install it or: pip install imageio-ffmpeg"
    )


def list_microphones(ffmpeg: str | None = None) -> list[str]:
    """DirectShow audio capture devices (Windows)."""
    exe = ffmpeg or find_ffmpeg()
    proc = subprocess.run(
        [exe, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    text = (proc.stderr or "") + (proc.stdout or "")
    devices: list[str] = []
    in_audio_section = False
    for line in text.splitlines():
        lower = line.lower()
        if "alternative name" in lower:
            continue
        if "directshow audio devices" in lower:
            in_audio_section = True
            continue
        if "directshow video devices" in lower:
            in_audio_section = False
            continue
        match = re.search(r'"([^"]+)"', line)
        if not match:
            continue
        name = match.group(1).strip()
        if not name:
            continue
        # ffmpeg 7+: `"Mic" (audio)` mixed into one device list
        tagged_audio = bool(re.search(r"\(audio\)\s*$", line.strip(), re.I))
        if tagged_audio or in_audio_section:
            if name not in devices:
                devices.append(name)
    return devices


_LOOPBACK_HINTS = ("stereo mix", "what u hear", "loopback", "wave out")


def preferred_microphone(mics: list[str]) -> str | None:
    """First real capture device, skipping mix/loopback devices when possible."""
    for name in mics:
        lower = name.lower()
        if not any(hint in lower for hint in _LOOPBACK_HINTS):
            return name
    return mics[0] if mics else None


def default_output_path(
    ratio: str, quality: str, width: int, height: int, fps: int | None = None
) -> Path:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safe_ratio = ratio.replace(":", "x")
    fps_part = f"_{int(fps)}fps" if fps else ""
    name = f"{stamp}_{safe_ratio}_{quality}_{width}x{height}{fps_part}.mp4"
    return RECORDINGS_DIR / name


class ScreenRecorder:
    """Grab the cursor-centered box each frame and encode with optional mic audio."""

    def __init__(
        self,
        width: int,
        height: int,
        fps: int,
        crf: int,
        x264_preset: str,
        mic_name: str | None,
        audio_bitrate: str,
        output_path: Path,
        get_frame,
    ) -> None:
        self.width = even(max(50, width))
        self.height = even(max(50, height))
        self.fps = max(8, int(fps))
        self.crf = int(crf)
        self.x264_preset = x264_preset
        self.mic_name = mic_name or None
        self.audio_bitrate = audio_bitrate
        self.output_path = Path(output_path)
        self.get_frame = get_frame

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._write_thread: threading.Thread | None = None
        self._err_thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._error: str | None = None
        self._stderr_chunks: list[bytes] = []
        self._frame_q: queue.Queue[bytes | None] = queue.Queue(maxsize=2)
        self.started_at: float | None = None
        self.frames_written = 0
        self.frames_dropped = 0

    def start(self, ffmpeg: str) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        # Wall-clock timestamps so duration follows real recording time even if
        # 4K/60fps encoding cannot take every frame. CFR duplicates the last
        # frame to fill gaps. Do not use +faststart here: that remux can get
        # killed and leave a file that only plays the last few seconds.
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-video_size",
            f"{self.width}x{self.height}",
            "-use_wallclock_as_timestamps",
            "1",
            "-thread_queue_size",
            "64",
            "-i",
            "pipe:0",
        ]
        if self.mic_name:
            cmd += [
                "-f",
                "dshow",
                "-thread_queue_size",
                "4096",
                "-rtbufsize",
                "512M",
                "-i",
                f"audio={self.mic_name}",
            ]
        cmd += [
            "-map",
            "0:v:0",
        ]
        if self.mic_name:
            cmd += ["-map", "1:a:0"]
        cmd += [
            "-filter:v",
            "setpts=PTS-STARTPTS",
            "-c:v",
            "libx264",
            "-preset",
            self.x264_preset,
            "-crf",
            str(self.crf),
            "-pix_fmt",
            "yuv420p",
            "-tune",
            "zerolatency",
            "-fps_mode",
            "cfr",
            "-r",
            str(self.fps),
            "-g",
            str(self.fps),
        ]
        if self.mic_name:
            cmd += [
                "-c:a",
                "aac",
                "-b:a",
                self.audio_bitrate,
                "-ar",
                "48000",
                "-ac",
                "2",
                "-af",
                "aresample=async=1:first_pts=0",
                "-shortest",
            ]
        else:
            cmd += ["-an"]
        cmd.append(str(self.output_path))

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=CREATE_NO_WINDOW,
        )
        self._err_thread = threading.Thread(target=self._drain_stderr, name="ffmpeg-stderr", daemon=True)
        self._err_thread.start()
        self._write_thread = threading.Thread(target=self._write_loop, name="ffmpeg-stdin", daemon=True)
        self._write_thread.start()
        self.started_at = time.perf_counter()
        self._thread = threading.Thread(target=self._loop, name="screen-recorder", daemon=True)
        self._thread.start()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for chunk in iter(lambda: proc.stderr.read(4096), b""):
                self._stderr_chunks.append(chunk)
        except OSError:
            return

    def _write_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        stdin = proc.stdin
        try:
            while True:
                item = self._frame_q.get()
                if item is None:
                    break
                stdin.write(item)
                self.frames_written += 1
        except (BrokenPipeError, OSError) as exc:
            if not self._stop.is_set():
                self._error = str(exc)

    def _close_stdin(self) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass

    def stop(self, timeout: float = 60.0) -> Path | None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        try:
            self._frame_q.put_nowait(None)
        except queue.Full:
            try:
                self._frame_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._frame_q.put_nowait(None)
            except queue.Full:
                pass
        if self._write_thread is not None:
            self._write_thread.join(timeout=8.0)
        self._close_stdin()
        if self._write_thread is not None:
            self._write_thread.join(timeout=5.0)
        proc = self._proc
        if proc is not None:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            if self._err_thread is not None:
                self._err_thread.join(timeout=2)
            err_text = b"".join(self._stderr_chunks).decode("utf-8", errors="replace").strip()
            if proc.returncode not in (0, None) and err_text and not self._error:
                self._error = err_text
            self._proc = None
        if self._error:
            raise RuntimeError(self._error)
        if self.output_path.exists() and self.output_path.stat().st_size > 0:
            return self.output_path
        return None

    def elapsed_s(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.perf_counter() - self.started_at

    def _loop(self) -> None:
        import mss

        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        import numpy as np

        interval = 1.0 / float(self.fps)
        next_t = time.perf_counter()
        out_frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        grab_buf = np.zeros((self.height, self.width, 3), dtype=np.uint8)

        try:
            with mss.mss() as sct:
                virt = sct.monitors[0]
                virt_l = int(virt["left"])
                virt_t = int(virt["top"])
                virt_r = virt_l + int(virt["width"])
                virt_b = virt_t + int(virt["height"])
                try:
                    sct.grab(virt)
                except Exception:
                    pass
                next_t = time.perf_counter()
                while not self._stop.is_set():
                    now = time.perf_counter()
                    if now < next_t:
                        time.sleep(min(0.002, next_t - now))
                        continue
                    while next_t < now - interval:
                        next_t += interval
                    if self._frame_q.full():
                        self.frames_dropped += 1
                        next_t += interval
                        continue
                    cx, cy, cap_w, cap_h = self.get_frame()
                    cap_w = even(max(64, int(cap_w)))
                    cap_h = even(max(64, int(cap_h)))
                    if grab_buf.shape[0] != cap_h or grab_buf.shape[1] != cap_w:
                        grab_buf = np.zeros((cap_h, cap_w, 3), dtype=np.uint8)
                    x = int(cx) - (cap_w // 2)
                    y = int(cy) - (cap_h // 2)
                    captured = _grab_padded(
                        sct, grab_buf, x, y, cap_w, cap_h, virt_l, virt_t, virt_r, virt_b
                    )
                    if cap_w == self.width and cap_h == self.height:
                        frame = captured
                    else:
                        frame = _resize_bgr(captured, out_frame)
                    payload = np.ascontiguousarray(frame).tobytes()
                    try:
                        self._frame_q.put_nowait(payload)
                    except queue.Full:
                        self.frames_dropped += 1
                    next_t += interval
                    if proc.poll() is not None:
                        self._error = self._error or (
                            b"".join(self._stderr_chunks).decode("utf-8", errors="replace").strip()
                            or f"ffmpeg exited with code {proc.returncode}"
                        )
                        break
        except Exception as exc:  # noqa: BLE001 — surface any capture failure on stop()
            self._error = str(exc)
        try:
            self._frame_q.put_nowait(None)
        except queue.Full:
            pass


def _grab_padded(
    sct,
    canvas,
    x: int,
    y: int,
    w: int,
    h: int,
    virt_l: int,
    virt_t: int,
    virt_r: int,
    virt_b: int,
):
    """Capture [x,y,w,h], padding with black where the box leaves the virtual screen."""
    import numpy as np

    left = max(x, virt_l)
    top = max(y, virt_t)
    right = min(x + w, virt_r)
    bottom = min(y + h, virt_b)
    if right <= left or bottom <= top:
        canvas.fill(0)
        return canvas
    shot = np.asarray(
        sct.grab({"left": left, "top": top, "width": right - left, "height": bottom - top})
    )
    dest_x = left - x
    dest_y = top - y
    sh, sw = shot.shape[0], shot.shape[1]
    if dest_x == 0 and dest_y == 0 and sw == w and sh == h:
        canvas[:, :, :] = shot[:, :, :3]
        return canvas
    canvas.fill(0)
    canvas[dest_y : dest_y + sh, dest_x : dest_x + sw, :] = shot[:, :, :3]
    return canvas


def _resize_bgr(src, dst):
    """Nearest-neighbor resize into dst (H, W, 3), keeping the output aspect ratio."""
    import numpy as np

    nh, nw = dst.shape[0], dst.shape[1]
    h, w = src.shape[0], src.shape[1]
    if h == nh and w == nw:
        dst[:, :, :] = src
        return dst
    ys = (np.arange(nh) * (h / nh)).astype(np.intp)
    xs = (np.arange(nw) * (w / nw)).astype(np.intp)
    np.clip(ys, 0, h - 1, out=ys)
    np.clip(xs, 0, w - 1, out=xs)
    dst[:, :, :] = src[ys[:, None], xs]
    return dst


def ensure_recording_deps() -> None:
    """Install mss / numpy / imageio-ffmpeg if missing (first-run convenience)."""
    missing: list[str] = []
    try:
        import mss  # noqa: F401
    except ImportError:
        missing.append("mss")
    try:
        import numpy  # noqa: F401
    except ImportError:
        missing.append("numpy")
    try:
        import imageio_ffmpeg  # noqa: F401
    except ImportError:
        missing.append("imageio-ffmpeg")
    if not missing:
        return
    print("  Installing: " + ", ".join(missing) + " ...")
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing]
    subprocess.check_call(cmd)
    print()
