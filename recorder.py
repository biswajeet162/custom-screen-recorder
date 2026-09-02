"""
Cursor-following screen recorder: captures the framed region and mic audio via ffmpeg.
"""

from __future__ import annotations

import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
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
NO_CAMERA = "__none__"

CAMERA_SHAPES = ("square", "circle")
CAMERA_POSITIONS = ("top_left", "top_right", "bottom_left", "bottom_right")
CAMERA_SIZES: dict[str, float] = {
    "small": 0.14,
    "medium": 0.20,
    "large": 0.28,
}
CAMERA_SIZE_LABELS = {
    "small": "Small",
    "medium": "Medium  (recommended)",
    "large": "Large",
}
CAMERA_POSITION_LABELS = {
    "top_left": "Top left",
    "top_right": "Top right",
    "bottom_left": "Bottom left",
    "bottom_right": "Bottom right",
}


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


def _ffmpeg_dshow_video_devices(ffmpeg: str | None = None) -> list[str]:
    """DirectShow video devices reported by ffmpeg."""
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
    in_video_section = False
    for line in text.splitlines():
        lower = line.lower()
        if "alternative name" in lower:
            continue
        if "directshow video devices" in lower:
            in_video_section = True
            continue
        if "directshow audio devices" in lower:
            in_video_section = False
            continue
        match = re.search(r'"([^"]+)"', line)
        if not match:
            continue
        name = match.group(1).strip()
        if not name:
            continue
        tagged_video = bool(re.search(r"\(video\)\s*$", line.strip(), re.I))
        if tagged_video or in_video_section:
            if name not in devices:
                devices.append(name)
    return devices


def _pnp_video_devices() -> dict[str, str]:
    """Windows PnP cameras, including Media Foundation-only devices like DroidCam Video."""
    if sys.platform != "win32":
        return {}
    ps = (
        "Get-PnpDevice -ErrorAction SilentlyContinue | "
        "Where-Object { ($_.Class -eq 'Camera') -or "
        "($_.Class -eq 'MEDIA' -and $_.FriendlyName -match 'DroidCam|Webcam|Video') } | "
        "Where-Object { $_.FriendlyName -notmatch 'Audio|Microphone|Effect|Studio' } | "
        "ForEach-Object { $_.FriendlyName + '|' + $_.Status }"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    devices: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        if "|" not in line:
            continue
        name, status = line.split("|", 1)
        name = name.strip()
        status = status.strip()
        if name:
            devices[name] = status
    return devices


def _scan_msmf_indices(max_idx: int = 16) -> list[int]:
    """OpenCV MSMF indices that currently deliver a frame."""
    import cv2

    working: list[int] = []
    for idx in range(max_idx):
        cap = cv2.VideoCapture(idx, cv2.CAP_MSMF)
        if not cap.isOpened():
            cap.release()
            continue
        ok, frame = cap.read()
        cap.release()
        if ok and frame is not None:
            working.append(idx)
        time.sleep(0.05)
    return working


DROIDCAM_HTTP_URLS = (
    "http://127.0.0.1:4747/video",
    "http://localhost:4747/video",
    "http://127.0.0.1:4747/mjpegfeed",
    "http://localhost:4747/mjpegfeed",
    "http://127.0.0.1:4747/mjpegfeed?640x480",
    "http://localhost:4747/mjpegfeed?640x480",
)


def _probe_http_stream(url: str) -> bool:
    """Return True if an HTTP MJPEG/VideoCapture URL delivers a frame."""
    import cv2

    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        cap.release()
        return False
    ok, frame = cap.read()
    cap.release()
    return bool(ok and frame is not None)


def _droidcam_client_listening() -> bool:
    """True when the DroidCam PC client is serving video on the default port."""
    for host in ("127.0.0.1", "localhost"):
        try:
            with socket.create_connection((host, 4747), timeout=0.35):
                return True
        except OSError:
            continue
    return False


def find_droidcam_http_url() -> str | None:
    """DroidCam PC client exposes the phone camera on localhost when streaming."""
    if not _droidcam_client_listening():
        return None
    for url in DROIDCAM_HTTP_URLS:
        if _probe_http_stream(url):
            return url
    return None


@dataclass
class CameraDevice:
    name: str
    backend: str  # "dshow" | "msmf" | "http"
    index: int = -1
    pnp_status: str | None = None
    source_url: str | None = None

    @property
    def driver_ok(self) -> bool:
        if self.backend == "http" and self.source_url:
            return True
        if not self.pnp_status:
            return True
        return self.pnp_status.lower() in ("ok", "unknown")

    @property
    def can_try_open(self) -> bool:
        if self.backend == "http" and self.source_url:
            return True
        return self.driver_ok


def _assign_msmf_indices(devices: list[CameraDevice]) -> None:
    msmf = _scan_msmf_indices()
    if not msmf:
        return
    mf_only = [d for d in devices if d.backend == "msmf"]
    if not mf_only:
        return
    pool = list(msmf)
    has_dshow_integrated = any(
        d.backend == "dshow"
        and any(
            hint in d.name.lower()
            for hint in ("integrated", "built-in", "facetime", "laptop", "hd user facing")
        )
        for d in devices
    )
    if has_dshow_integrated and len(pool) > 1 and len(mf_only) <= len(pool) - 1:
        pool = pool[1:]
    if len(mf_only) == 1:
        mf_only[0].index = pool[0] if pool else msmf[0]
        return
    for dev, idx in zip(mf_only, pool):
        dev.index = idx


def discover_cameras(ffmpeg: str | None = None) -> list[CameraDevice]:
    """All video cameras: DirectShow + Media Foundation + DroidCam HTTP stream."""
    dshow = _ffmpeg_dshow_video_devices(ffmpeg)
    pnp = _pnp_video_devices()
    by_key: dict[str, CameraDevice] = {}
    for idx, name in enumerate(dshow):
        by_key[name.lower()] = CameraDevice(name, "dshow", idx, pnp.get(name))
    for name, status in pnp.items():
        key = name.lower()
        if key not in by_key:
            by_key[key] = CameraDevice(name, "msmf", -1, status)
    devices = list(by_key.values())
    _assign_msmf_indices(devices)

    droidcam_url = find_droidcam_http_url()
    if droidcam_url:
        wired = False
        for dev in devices:
            if "droid" in dev.name.lower():
                dev.backend = "http"
                dev.source_url = droidcam_url
                dev.index = -1
                wired = True
                break
        if not wired:
            devices.append(
                CameraDevice(
                    "DroidCam (phone stream)",
                    "http",
                    -1,
                    "OK",
                    droidcam_url,
                )
            )
    return devices


def find_camera(name: str, catalog: list[CameraDevice]) -> CameraDevice | None:
    key = name.lower()
    for dev in catalog:
        if dev.name.lower() == key:
            return dev
    return None


def _resolve_msmf_index(device: CameraDevice, catalog: list[CameraDevice]) -> int | None:
    if device.index >= 0:
        return device.index
    msmf = _scan_msmf_indices()
    if not msmf:
        return None
    mf_only = [d for d in catalog if d.backend == "msmf"]
    if len(mf_only) == 1 and len(msmf) == 1:
        return msmf[0]
    if "droid" in device.name.lower():
        for idx in reversed(msmf):
            if idx > 0:
                return idx
        return msmf[-1]
    return msmf[0]


def probe_cameras(ffmpeg: str | None = None) -> list[str]:
    """Return camera names that open and deliver a frame right now."""
    working: list[str] = []
    catalog = discover_cameras(ffmpeg)
    for dev in catalog:
        if not dev.can_try_open:
            continue
        try:
            cap = WebcamCapture.open_device(dev, catalog=catalog, retries=2, delay_s=0.25)
            cap.stop()
            working.append(dev.name)
        except Exception:
            pass
        time.sleep(0.12)
    return working


def list_cameras(ffmpeg: str | None = None) -> list[str]:
    """All detected video capture devices (DirectShow + Media Foundation)."""
    return [dev.name for dev in discover_cameras(ffmpeg)]


_LOOPBACK_HINTS = ("stereo mix", "what u hear", "loopback", "wave out")
_CAMERA_SKIP_HINTS = ("obs", "virtual", "snap camera", "manycam", "avatar")


def preferred_microphone(mics: list[str]) -> str | None:
    """First real capture device, skipping mix/loopback devices when possible."""
    for name in mics:
        lower = name.lower()
        if not any(hint in lower for hint in _LOOPBACK_HINTS):
            return name
    return mics[0] if mics else None


def preferred_camera(cameras: list[str]) -> str | None:
    """Pick a built-in / webcam device when possible."""
    for name in cameras:
        lower = name.lower()
        if any(hint in lower for hint in _CAMERA_SKIP_HINTS):
            continue
        if any(hint in lower for hint in ("integrated", "built-in", "facetime", "laptop", "hd user facing")):
            return name
    for name in cameras:
        lower = name.lower()
        if not any(hint in lower for hint in _CAMERA_SKIP_HINTS):
            return name
    return cameras[0] if cameras else None


def overlay_source_dims(
    frame_w: int,
    frame_h: int,
    rotation_deg: int = 0,
) -> tuple[int, int]:
    """Display width/height after rotation (90°/270° swap axes)."""
    rot = int(rotation_deg) % 360
    if rot in (90, 270):
        return frame_h, frame_w
    return frame_w, frame_h


def apply_camera_rotation(frame, rotation_deg: int = 0):
    """Rotate a BGR frame in 90° steps."""
    import cv2

    rot = int(rotation_deg) % 360
    if rot == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if rot == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if rot == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return frame


def camera_overlay_pixels(
    out_w: int,
    out_h: int,
    size_key: str,
    position_key: str,
    ox: int | None = None,
    oy: int | None = None,
    frame_w: int | None = None,
    frame_h: int | None = None,
    zoom: float = 1.0,
    rotation_deg: int = 0,
) -> tuple[int, int, int, int]:
    """Return cam_w, cam_h, x, y on the encoded frame (native camera aspect)."""
    frac = CAMERA_SIZES.get(size_key, CAMERA_SIZES["medium"])
    max_h = max(48, int(out_h * frac))
    max_w = max(48, int(out_w * frac))
    zoom = max(0.5, min(4.0, float(zoom)))
    if frame_w and frame_h and frame_w > 0 and frame_h > 0:
        frame_w, frame_h = overlay_source_dims(int(frame_w), int(frame_h), rotation_deg)
        aspect = frame_w / frame_h
        if aspect >= 1.0:
            cam_h = even(max_h)
            cam_w = even(min(max_w, int(cam_h * aspect)))
            if cam_w > max_w:
                cam_w = even(max_w)
                cam_h = even(max(48, int(cam_w / aspect)))
        else:
            cam_h = even(min(max_h, int(max_w / aspect)))
            cam_w = even(max(48, int(cam_h * aspect)))
            if cam_w > max_w:
                cam_w = even(max_w)
                cam_h = even(max(48, int(cam_w / aspect)))
    else:
        cam_w = cam_h = even(max_h)

    if zoom < 1.0:
        cam_w = even(max(48, int(cam_w * zoom)))
        cam_h = even(max(48, int(cam_h * zoom)))

    margin = even(max(8, int(min(out_w, out_h) * 0.03)))
    if ox is not None and oy is not None:
        ox_i = max(0, min(int(ox), out_w - cam_w))
        oy_i = max(0, min(int(oy), out_h - cam_h))
        return cam_w, cam_h, ox_i, oy_i
    if position_key == "top_left":
        ox_i, oy_i = margin, margin
    elif position_key == "top_right":
        ox_i, oy_i = out_w - cam_w - margin, margin
    elif position_key == "bottom_left":
        ox_i, oy_i = margin, out_h - cam_h - margin
    else:
        ox_i, oy_i = out_w - cam_w - margin, out_h - cam_h - margin
    ox_i = max(0, min(int(ox_i), out_w - cam_w))
    oy_i = max(0, min(int(oy_i), out_h - cam_h))
    return cam_w, cam_h, ox_i, oy_i


def prepare_webcam_patch(
    cam_bgr,
    box_w: int,
    box_h: int,
    shape: str,
    zoom: float = 1.0,
    rotation_deg: int = 0,
):
    """Aspect-correct webcam patch with center zoom and optional rotation."""
    import cv2
    import numpy as np

    box_w = max(1, int(box_w))
    box_h = max(1, int(box_h))
    zoom = max(0.5, min(4.0, float(zoom)))
    src = apply_camera_rotation(cam_bgr, rotation_deg)
    sh, sw = src.shape[:2]

    if zoom > 1.0:
        crop_w = max(1, int(sw / zoom))
        crop_h = max(1, int(sh / zoom))
        x0 = max(0, (sw - crop_w) // 2)
        y0 = max(0, (sh - crop_h) // 2)
        src = src[y0:y0 + crop_h, x0:x0 + crop_w]

    sh, sw = src.shape[:2]
    scale = min(box_w / sw, box_h / sh)
    nw = max(1, int(sw * scale))
    nh = max(1, int(sh * scale))
    resized = cv2.resize(src, (nw, nh), interpolation=cv2.INTER_AREA)

    patch = np.zeros((box_h, box_w, 3), dtype=np.uint8)
    x_off = (box_w - nw) // 2
    y_off = (box_h - nh) // 2
    patch[y_off:y_off + nh, x_off:x_off + nw] = resized

    if shape == "circle":
        mask = np.zeros((box_h, box_w), dtype=np.float32)
        radius = min(box_w, box_h) / 2.0 - 1.0
        cv2.circle(mask, (box_w // 2, box_h // 2), int(max(1, radius)), 1.0, -1)
        for c in range(3):
            patch[:, :, c] = (patch[:, :, c] * mask).astype(np.uint8)
    return patch


def screen_camera_preview_rect(
    box_x: int,
    box_y: int,
    box_w: int,
    box_h: int,
    encode_w: int,
    encode_h: int,
    size_key: str,
    position_key: str,
    ox: int | None = None,
    oy: int | None = None,
) -> tuple[int, int, int, int]:
    """On-screen preview window matching the recorded overlay."""
    cam_w, cam_h, ox_i, oy_i = camera_overlay_pixels(
        encode_w, encode_h, size_key, position_key, ox=ox, oy=oy
    )
    if encode_w <= 0 or encode_h <= 0:
        return box_x, box_y, 120, 120
    screen_w = max(48, int(box_w * cam_w / encode_w))
    screen_h = max(48, int(box_h * cam_h / encode_h))
    screen_ox = int(box_x + box_w * ox_i / encode_w)
    screen_oy = int(box_y + box_h * oy_i / encode_h)
    return screen_ox, screen_oy, screen_w, screen_h


def composite_webcam_onto(
    dst,
    cam_bgr,
    size_key: str,
    position_key: str,
    ox: int,
    oy: int,
    shape: str,
    zoom: float = 1.0,
    rotation_deg: int = 0,
) -> None:
    """Blend a webcam frame onto a BGR screen frame (native aspect, center zoom)."""
    dh, dw = dst.shape[:2]
    fh, fw = cam_bgr.shape[:2]
    cam_w, cam_h, ox_i, oy_i = camera_overlay_pixels(
        dw,
        dh,
        size_key,
        position_key,
        ox=ox,
        oy=oy,
        frame_w=fw,
        frame_h=fh,
        zoom=zoom,
        rotation_deg=rotation_deg,
    )
    if ox_i >= dw or oy_i >= dh or ox_i + cam_w <= 0 or oy_i + cam_h <= 0:
        return
    patch = prepare_webcam_patch(cam_bgr, cam_w, cam_h, shape, zoom, rotation_deg)
    x0 = max(0, ox_i)
    y0 = max(0, oy_i)
    x1 = min(dw, ox_i + cam_w)
    y1 = min(dh, oy_i + cam_h)
    sx0 = x0 - ox_i
    sy0 = y0 - oy_i
    sx1 = sx0 + (x1 - x0)
    sy1 = sy0 + (y1 - y0)
    dst[y0:y1, x0:x1] = patch[sy0:sy1, sx0:sx1]


class WebcamCapture:
    """Threaded webcam reader (DirectShow, Media Foundation, or HTTP/MJPEG)."""

    def __init__(
        self,
        device_index: int = -1,
        device_name: str | None = None,
        *,
        backend: str = "dshow",
        source_url: str | None = None,
    ) -> None:
        import cv2

        self._lock = threading.Lock()
        self._frame = None
        self._stop = threading.Event()
        self._device_index = int(device_index)
        self._device_name = device_name
        self._backend = backend
        self._source_url = source_url
        if backend == "http" and source_url:
            cap = cv2.VideoCapture(source_url, cv2.CAP_FFMPEG)
        else:
            api = cv2.CAP_MSMF if backend == "msmf" else cv2.CAP_DSHOW
            cap = cv2.VideoCapture(int(device_index), api)
        self._api = cv2.CAP_FFMPEG if backend == "http" else (
            cv2.CAP_MSMF if backend == "msmf" else cv2.CAP_DSHOW
        )
        if not cap.isOpened():
            cap.release()
            label = device_name or source_url or f"index {device_index}"
            raise RuntimeError(f"Could not open camera {label}")
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            label = device_name or source_url or f"index {device_index}"
            raise RuntimeError(f"Could not read from camera {label}")
        self._cap = cap
        with self._lock:
            self._frame = frame
        try:
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        self._thread: threading.Thread | None = None

    @classmethod
    def open(
        cls,
        device_index: int,
        device_name: str | None = None,
        *,
        backend: str = "dshow",
        retries: int = 8,
        delay_s: float = 0.5,
    ) -> "WebcamCapture":
        last_err: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                cap = cls(device_index, device_name, backend=backend)
                cap.start()
                return cap
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt + 1 < retries:
                    time.sleep(delay_s)
        raise RuntimeError(str(last_err or "Could not open camera"))

    @classmethod
    def open_url(
        cls,
        source_url: str,
        device_name: str | None = None,
        *,
        retries: int = 8,
        delay_s: float = 0.5,
    ) -> "WebcamCapture":
        last_err: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                cap = cls(-1, device_name, backend="http", source_url=source_url)
                cap.start()
                return cap
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt + 1 < retries:
                    time.sleep(delay_s)
        raise RuntimeError(str(last_err or "Could not open camera stream"))

    @classmethod
    def open_device(
        cls,
        device: CameraDevice,
        *,
        catalog: list[CameraDevice] | None = None,
        retries: int = 8,
        delay_s: float = 0.5,
    ) -> "WebcamCapture":
        if device.backend == "http" and device.source_url:
            return cls.open_url(
                device.source_url,
                device.name,
                retries=retries,
                delay_s=delay_s,
            )
        if "droid" in device.name.lower():
            url = find_droidcam_http_url()
            if url:
                return cls.open_url(url, device.name, retries=retries, delay_s=delay_s)
        backend = device.backend
        index = device.index
        if backend == "msmf" and index < 0:
            index = _resolve_msmf_index(device, catalog or [device]) or -1
        if index < 0:
            label = device.name
            msg = f"Could not open camera {label}"
            if "droid" in device.name.lower():
                msg += (
                    ". Start the DroidCam client on your PC, press Start so video is "
                    "streaming, then select this camera again."
                )
            elif device.pnp_status and device.pnp_status.lower() == "error":
                msg += (
                    ". The camera driver has an error in Device Manager — "
                    "disable then enable the device, or reinstall its software."
                )
            raise RuntimeError(msg)
        return cls.open(index, device.name, backend=backend, retries=retries, delay_s=delay_s)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="webcam-capture", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            cap = self._cap
            if cap is None or not cap.isOpened():
                time.sleep(0.05)
                continue
            ok, frame = cap.read()
            if ok and frame is not None:
                with self._lock:
                    self._frame = frame
            else:
                time.sleep(0.02)

    def get_frame(self):
        with self._lock:
            if self._frame is None:
                return None
            return self._frame.copy()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            if self._cap is not None:
                self._cap.release()
        except Exception:
            pass
        self._cap = None


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
        webcam: WebcamCapture | None = None,
        camera_shape: str = "circle",
        camera_size: str = "medium",
        camera_position: str = "bottom_right",
        camera_ox: int | None = None,
        camera_oy: int | None = None,
        camera_zoom: float = 1.0,
        camera_rotation: int = 0,
        camera_state: dict | None = None,
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
        self._webcam = webcam
        self._camera_shape = camera_shape if camera_shape in CAMERA_SHAPES else "circle"
        self._camera_size = camera_size if camera_size in CAMERA_SIZES else "medium"
        self._camera_position = (
            camera_position if camera_position in CAMERA_POSITIONS else "bottom_right"
        )
        if camera_state is not None:
            self._camera_state = camera_state
        else:
            self._camera_state = {
                "ox": camera_ox,
                "oy": camera_oy,
                "zoom": max(0.5, min(4.0, float(camera_zoom))),
                "rotation": int(camera_rotation) % 360,
                "visible": True,
                "position": self._camera_position,
            }

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
                    if self._webcam is not None and self._camera_state.get("visible", True):
                        cam_frame = self._webcam.get_frame()
                        if cam_frame is not None:
                            ox = int(self._camera_state.get("ox") or 0)
                            oy = int(self._camera_state.get("oy") or 0)
                            zoom = float(self._camera_state.get("zoom", 1.0))
                            rotation = int(self._camera_state.get("rotation", 0))
                            position = str(
                                self._camera_state.get("position") or self._camera_position
                            )
                            composite_webcam_onto(
                                frame,
                                cam_frame,
                                self._camera_size,
                                position,
                                ox,
                                oy,
                                self._camera_shape,
                                zoom,
                                rotation,
                            )
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
    """Install capture/encode packages if missing (first-run convenience)."""
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
    try:
        import cv2  # noqa: F401
    except ImportError:
        missing.append("opencv-python-headless")
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        missing.append("Pillow")
    if not missing:
        return
    print("  Installing: " + ", ".join(missing) + " ...")
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing]
    subprocess.check_call(cmd)
    print()
