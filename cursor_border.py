"""
Cursor-centered aspect-ratio border overlay for Windows.

Shows a fullscreen two-column setup window with a live cyan recording frame and
webcam overlay on screen, then records inside that frame with the chosen microphone.
Press Ctrl+Caps Lock to park the border.
Press Ctrl+Caps Lock again to follow the pointer.
Hold Ctrl+Shift and Right to zoom the border in, or Left to zoom out.
Press Ctrl+Caps Lock to park the border (then drag the camera inside the frame).
Customize shortcuts from Settings in the setup window. Press Esc to stop and save.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes

# Must run before tkinter / mss create windows, or Windows will stretch the
# preview and then shrink the recording box when capture becomes DPI-aware.
PROCESS_PER_MONITOR_DPI_AWARE = 2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_user32_early = ctypes.windll.user32
try:
    _user32_early.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
    except Exception:
        try:
            _user32_early.SetProcessDPIAware()
        except Exception:
            pass

import tkinter as tk
from tkinter import messagebox, ttk

from recorder import (
    NO_AUDIO,
    NO_CAMERA,
    FPS_CHOICES,
    CAMERA_POSITION_LABELS,
    CAMERA_POSITIONS,
    CAMERA_SHAPES,
    CAMERA_SIZE_LABELS,
    CAMERA_SIZES,
    QUALITY_PRESETS,
    ScreenRecorder,
    WebcamCapture,
    CameraDevice,
    camera_overlay_pixels,
    default_output_path,
    discover_cameras,
    encoder_preset,
    ensure_recording_deps,
    even,
    find_camera,
    find_ffmpeg,
    list_microphones,
    preferred_camera,
    preferred_microphone,
    prepare_webcam_patch,
    probe_cameras,
    overlay_source_dims,
    screen_camera_preview_rect,
    size_for_quality,
)

user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
LWA_COLORKEY = 0x00000001
VK_ESCAPE = 0x1B
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_CAPITAL = 0x14
VK_LEFT = 0x25
VK_RIGHT = 0x27
VK_UP = 0x26
VK_DOWN = 0x28
VK_MENU = 0x12
VK_NUMPAD0 = 0x60
VK_NUMPAD1 = 0x61
VK_NUMPAD3 = 0x63
VK_NUMPAD7 = 0x67
VK_NUMPAD9 = 0x69
VK_0 = 0x30

DEFAULT_SHORTCUTS: dict[str, str] = {
    "park_follow": "Ctrl+Caps",
    "zoom_in": "Ctrl+Shift+Right",
    "zoom_out": "Ctrl+Shift+Left",
    "cam_top_left": "Ctrl+Numpad7",
    "cam_top_right": "Ctrl+Numpad9",
    "cam_bottom_left": "Ctrl+Numpad1",
    "cam_bottom_right": "Ctrl+Numpad3",
    "cam_toggle": "Ctrl+0",
}

SHORTCUT_LABELS: dict[str, str] = {
    "park_follow": "Park / follow border (drag camera while parked)",
    "zoom_in": "Zoom border in",
    "zoom_out": "Zoom border out",
    "cam_top_left": "Camera → top left",
    "cam_top_right": "Camera → top right",
    "cam_bottom_left": "Camera → bottom left",
    "cam_bottom_right": "Camera → bottom right",
    "cam_toggle": "Show / hide camera",
}

_KEY_ALIASES: dict[str, int] = {
    "left": VK_LEFT,
    "right": VK_RIGHT,
    "up": VK_UP,
    "down": VK_DOWN,
    "0": VK_0,
    "numpad0": VK_NUMPAD0,
    "numpad1": VK_NUMPAD1,
    "numpad3": VK_NUMPAD3,
    "numpad7": VK_NUMPAD7,
    "numpad9": VK_NUMPAD9,
}


def _normalize_hotkey_part(part: str) -> str:
    p = part.strip().lower().replace(" ", "")
    if p in ("control", "ctl"):
        return "ctrl"
    if p in ("capslock", "cap"):
        return "caps"
    return p


def parse_hotkey(text: str) -> dict[str, object]:
    """Parse 'Ctrl+Shift+Right' into modifier flags and a virtual-key code."""
    parts = [_normalize_hotkey_part(p) for p in str(text or "").split("+") if p.strip()]
    ctrl = "ctrl" in parts
    shift = "shift" in parts
    alt = "alt" in parts
    caps = "caps" in parts
    vk: int | None = None
    for part in parts:
        if part in ("ctrl", "shift", "alt", "caps"):
            continue
        if part in _KEY_ALIASES:
            vk = _KEY_ALIASES[part]
            break
        if len(part) == 1 and part.isdigit():
            vk = VK_0 + (int(part) - 0)
            break
    return {"ctrl": ctrl, "shift": shift, "alt": alt, "caps": caps, "vk": vk}


def modifiers_match(binding: dict[str, object], ctrl: bool, shift: bool, alt: bool, caps: bool) -> bool:
    if binding.get("ctrl") and not ctrl:
        return False
    if binding.get("shift") and not shift:
        return False
    if binding.get("alt") and not alt:
        return False
    if binding.get("caps") and not caps:
        return False
    return True


def key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(int(vk)) & 0x8000)


def key_edge(vk: int, was_down: dict[int, bool]) -> bool:
    down = key_down(vk)
    prev = was_down.get(vk, False)
    was_down[vk] = down
    return down and not prev
MONITOR_DEFAULTTONEAREST = 2
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_SHOWWINDOW = 0x0040
GA_ROOT = 2
WDA_EXCLUDEFROMCAPTURE = 0x00000011

# =============================================================================
# CONFIG — used by overlay-only mode and as custom-size defaults
# =============================================================================

# Option 1 — 16:9 (widescreen)
OPTION1_WIDTH = 1280
OPTION1_HEIGHT = 720

# Option 2 — 9:16 (vertical / portrait)
OPTION2_WIDTH = 720
OPTION2_HEIGHT = 1280

# Border look
BORDER_WIDTH = 3
BORDER_COLOR = "#00FFFF"  # cyan
BORDER_COLOR_LOCKED = "#CCFFFF"  # pale cyan when parked (Ctrl+Caps Lock)

# =============================================================================

KEY_COLOR = "#010101"
KEY_COLORREF = 0x00010101  # 0x00bbggrr for RGB(1,1,1)
UPDATE_MS = 8  # ~120 FPS — keeps cursor locked to box center
ZOOM_RATE = 1.55  # size multiplier per second while arrow keys are held (smooth)
ZOOM_MIN = 0.25
ZOOM_MAX = 8.0

# Setup window (fullscreen settings panel)
SETUP_BG = "#0b0e14"
SETUP_PANEL = "#12161f"
SETUP_CARD = "#1a2030"
SETUP_TEXT = "#eef2f8"
SETUP_MUTED = "#8b95a8"
SETUP_ACCENT = "#00d4ff"
SETUP_ACCENT_HOVER = "#33e0ff"
SETUP_BORDER = "#2a3344"
SETUP_SIDEBAR_W = 500


def configure_setup_theme(root: tk.Tk) -> ttk.Style:
    root.configure(bg=SETUP_BG)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background=SETUP_PANEL, foreground=SETUP_TEXT)
    style.configure("Setup.TFrame", background=SETUP_PANEL)
    style.configure("Setup.TLabel", background=SETUP_PANEL, foreground=SETUP_TEXT, font=("Segoe UI", 14))
    style.configure(
        "SetupMuted.TLabel",
        background=SETUP_PANEL,
        foreground=SETUP_MUTED,
        font=("Segoe UI", 12),
    )
    style.configure(
        "SetupTitle.TLabel",
        background=SETUP_PANEL,
        foreground=SETUP_TEXT,
        font=("Segoe UI", 18, "bold"),
    )
    style.configure(
        "SetupColTitle.TLabel",
        background=SETUP_PANEL,
        foreground=SETUP_ACCENT,
        font=("Segoe UI", 20, "bold"),
    )
    style.configure(
        "TCombobox",
        fieldbackground=SETUP_CARD,
        background=SETUP_CARD,
        foreground=SETUP_TEXT,
        arrowcolor=SETUP_TEXT,
        bordercolor=SETUP_BORDER,
        font=("Segoe UI", 13),
    )
    style.map("TCombobox", fieldbackground=[("readonly", SETUP_CARD)])
    style.configure(
        "TEntry",
        fieldbackground=SETUP_CARD,
        foreground=SETUP_TEXT,
        insertcolor=SETUP_TEXT,
        bordercolor=SETUP_BORDER,
        font=("Segoe UI", 13),
    )
    style.configure(
        "Horizontal.TScale",
        background=SETUP_PANEL,
        troughcolor=SETUP_CARD,
    )
    style.configure(
        "TRadiobutton",
        background=SETUP_PANEL,
        foreground=SETUP_TEXT,
        font=("Segoe UI", 10),
    )
    style.map(
        "TRadiobutton",
        background=[("active", SETUP_PANEL), ("selected", SETUP_PANEL)],
    )
    style.configure(
        "TScrollbar",
        background=SETUP_PANEL,
        troughcolor=SETUP_BG,
        bordercolor=SETUP_BORDER,
        arrowcolor=SETUP_MUTED,
    )
    return style


def setup_fullscreen_root(root: tk.Tk) -> None:
    sw = int(root.winfo_screenwidth())
    sh = int(root.winfo_screenheight())
    root.geometry(f"{sw}x{sh}+0+0")
    root.minsize(960, 640)
    try:
        root.state("zoomed")
    except tk.TclError:
        pass


def lift_preview_above_settings(root: tk.Tk, preview: CyanBorder) -> None:
    """Keep the cyan capture frame above the fullscreen settings panel."""
    try:
        preview.win.attributes("-topmost", True)
        preview.win.lift()
        root.attributes("-topmost", False)
        root.lift()
        preview.win.lift()
        if preview.hwnd:
            user32.SetWindowPos(
                preview.hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOMOVE,
            )
    except tk.TclError:
        pass


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


user32.MonitorFromPoint.argtypes = [POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HANDLE
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
user32.GetMonitorInfoW.restype = wintypes.BOOL


def enable_dpi_awareness() -> None:
    """Keep using real monitor pixels (safe to call more than once)."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def get_cursor_pos() -> tuple[int, int]:
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def virtual_screen_rect() -> tuple[int, int, int, int]:
    left = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    top = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    width = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    height = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    return left, top, left + width, top + height


def _monitor_info_at(x: int, y: int) -> MONITORINFO | None:
    pt = POINT(int(x), int(y))
    handle = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    if not handle:
        return None
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
        return None
    return info


def monitor_rect_at(x: int, y: int) -> tuple[int, int, int, int]:
    """Pixel bounds of the display that contains (x, y)."""
    info = _monitor_info_at(x, y)
    if info is None:
        return virtual_screen_rect()
    r = info.rcMonitor
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def monitor_work_rect_at(x: int, y: int) -> tuple[int, int, int, int]:
    """Visible work area (excludes the taskbar) of the display that contains (x, y)."""
    info = _monitor_info_at(x, y)
    if info is None:
        return virtual_screen_rect()
    r = info.rcWork
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def fit_size_to_rect(w: int, h: int, rect: tuple[int, int, int, int]) -> tuple[int, int]:
    """Shrink w×h to fit inside rect, keeping aspect ratio. Never upscale."""
    left, top, right, bottom = rect
    max_w = max(64, right - left)
    max_h = max(64, bottom - top)
    w = max(64, int(w))
    h = max(64, int(h))
    if w <= max_w and h <= max_h:
        return even(w), even(h)
    scale = min(max_w / w, max_h / h)
    return even(max(64, int(w * scale))), even(max(64, int(h * scale)))


def screen_fit(w: int, h: int, x: int | None = None, y: int | None = None) -> tuple[int, int]:
    """Fit a frame to the monitor under the cursor (or a given point)."""
    if x is None or y is None:
        x, y = get_cursor_pos()
    return fit_size_to_rect(w, h, monitor_rect_at(x, y))


def clamp_top_left(
    x: int, y: int, w: int, h: int, rect: tuple[int, int, int, int]
) -> tuple[int, int]:
    """Keep the whole w×h box inside rect so all four sides stay on that screen."""
    left, top, right, bottom = rect
    max_x = right - w
    max_y = bottom - h
    if max_x < left:
        x = left
    else:
        x = min(max(int(x), left), max_x)
    if max_y < top:
        y = top
    else:
        y = min(max(int(y), top), max_y)
    return int(x), int(y)


def pin_box_top_left(
    cx: int, cy: int, w: int, h: int, rect: tuple[int, int, int, int]
) -> tuple[int, int]:
    """Top-left of a w×h box aimed at (cx, cy), with near edges stuck on screen.

    While the box fits, it stays fully on the monitor and follows the point
    until an edge. If zoom makes it larger than the screen, the edges toward
    (cx, cy) stick to that corner; overflow goes off the opposite sides.
    """
    left, top, right, bottom = rect
    screen_w = right - left
    screen_h = bottom - top
    x = int(cx) - (int(w) // 2)
    y = int(cy) - (int(h) // 2)

    if w <= screen_w:
        x = min(max(x, left), right - w)
    else:
        t = 0.0 if screen_w <= 0 else (int(cx) - left) / screen_w
        t = min(max(t, 0.0), 1.0)
        x = int(round(left + t * (screen_w - w)))

    if h <= screen_h:
        y = min(max(y, top), bottom - h)
    else:
        t = 0.0 if screen_h <= 0 else (int(cy) - top) / screen_h
        t = min(max(t, 0.0), 1.0)
        y = int(round(top + t * (screen_h - h)))

    return int(x), int(y)


def configured_size(ratio: str) -> tuple[int, int]:
    """Return (width, height) from the CONFIG block for overlay-only mode."""
    if ratio == "16:9":
        return int(OPTION1_WIDTH), int(OPTION1_HEIGHT)
    return int(OPTION2_WIDTH), int(OPTION2_HEIGHT)


def get_hwnd(win: tk.Misc) -> int:
    """Resolve the real top-level HWND Tk uses for the overlay window."""
    hwnd = int(win.winfo_id())
    ancestor = int(user32.GetAncestor(hwnd, GA_ROOT))
    if ancestor:
        return ancestor
    parent = int(user32.GetParent(hwnd))
    return parent or hwnd


def _draw_inner_bars(canvas: tk.Canvas, w: int, h: int, bw: int, color: str) -> None:
    """Four cyan bars fully inside the window so right/bottom are never clipped."""
    canvas.delete("bar")
    bw = max(2, int(bw))
    # Keep the stroke inside the HWND. Layered windows clip the last screen pixel,
    # so inset by 2px and draw with line width (more reliable than edge rectangles).
    m = 2
    c = bw / 2.0
    x0, y0 = m, m
    x1, y1 = max(m + bw, w - m), max(m + bw, h - m)
    canvas.create_line(x0, y0 + c, x1, y0 + c, fill=color, width=bw, capstyle="projecting", tags="bar")
    canvas.create_line(x0, y1 - c, x1, y1 - c, fill=color, width=bw, capstyle="projecting", tags="bar")
    canvas.create_line(x0 + c, y0, x0 + c, y1, fill=color, width=bw, capstyle="projecting", tags="bar")
    canvas.create_line(x1 - c, y0, x1 - c, y1, fill=color, width=bw, capstyle="projecting", tags="bar")


class CyanBorder:
    """Click-through cyan rectangle. Border is drawn inside the frame (visible on all 4 sides)."""

    def __init__(
        self,
        master: tk.Misc | None,
        box_w: int,
        box_h: int,
        border_w: int,
        *,
        show_label: bool = False,
    ) -> None:
        self.border_w = max(2, min(30, int(border_w)))
        self.show_label = show_label
        self.box_w, self.box_h = screen_fit(box_w, box_h)
        self.hwnd = 0
        self.last_pos: tuple[int, int] | None = None
        self.color = BORDER_COLOR

        self._webcam_photo = None
        if master is None:
            self.win: tk.Misc = tk.Tk()
        else:
            self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=KEY_COLOR)
        self.win.geometry(f"{self.box_w}x{self.box_h}+0+0")

        self.canvas = tk.Canvas(
            self.win,
            width=self.box_w,
            height=self.box_h,
            bg=KEY_COLOR,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.win.update_idletasks()
        self.win.update()
        self.hwnd = get_hwnd(self.win)
        setup_layered_window(self.hwnd)
        try:
            self.win.attributes("-transparentcolor", KEY_COLOR)
        except tk.TclError:
            pass
        self._apply_pixel_size()
        self.redraw(self.color)
        try:
            self.canvas.tag_raise("webcam")
        except tk.TclError:
            pass

    def _apply_pixel_size(self, x: int | None = None, y: int | None = None) -> None:
        """Size/move with Win32 pixels (not Tk-scaled geometry)."""
        if not self.hwnd:
            return
        flags = SWP_NOACTIVATE | SWP_SHOWWINDOW
        if x is None or y is None:
            flags |= SWP_NOMOVE
            x, y = 0, 0
        user32.SetWindowPos(
            self.hwnd,
            HWND_TOPMOST,
            int(x),
            int(y),
            self.box_w,
            self.box_h,
            flags,
        )

    def redraw(self, color: str | None = None) -> None:
        if color is not None:
            self.color = color
        _draw_inner_bars(self.canvas, self.box_w, self.box_h, self.border_w, self.color)
        self.canvas.delete("sizelabel")
        if self.show_label:
            self.canvas.create_text(
                self.box_w // 2,
                self.border_w + 14,
                text=f"{self.box_w}  ×  {self.box_h}",
                fill=self.color,
                font=("Segoe UI", 16, "bold"),
                tags="sizelabel",
            )

    def set_size(self, box_w: int, box_h: int, *, fit_to_screen: bool = True) -> None:
        if fit_to_screen:
            self.box_w, self.box_h = screen_fit(box_w, box_h)
        else:
            self.box_w = even(max(64, box_w))
            self.box_h = even(max(64, box_h))
        self.canvas.config(width=self.box_w, height=self.box_h)
        self._apply_pixel_size()
        self.win.geometry(f"{self.box_w}x{self.box_h}")
        self.last_pos = None
        self.redraw()

    def move_top_left(self, x: int, y: int) -> None:
        if self.last_pos == (x, y):
            return
        self.last_pos = (x, y)
        if self.hwnd:
            user32.SetWindowPos(
                self.hwnd,
                HWND_TOPMOST,
                x,
                y,
                0,
                0,
                SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOZORDER,
            )
        self.win.geometry(f"+{x}+{y}")

    @property
    def top_left(self) -> tuple[int, int]:
        if self.last_pos is not None:
            return self.last_pos
        try:
            return int(self.win.winfo_rootx()), int(self.win.winfo_rooty())
        except tk.TclError:
            return 0, 0

    def clear_webcam_overlay(self) -> None:
        self.canvas.delete("webcam")
        self._webcam_photo = None

    def update_webcam_overlay(
        self,
        bgr_frame,
        encode_w: int,
        encode_h: int,
        cam_ox: int,
        cam_oy: int,
        shape: str,
        size_key: str,
        zoom: float = 1.0,
        rotation_deg: int = 0,
    ) -> None:
        """Draw the webcam inside this border (matches the recorded overlay position)."""
        import cv2
        import numpy as np
        from PIL import Image, ImageTk

        if encode_w <= 0 or encode_h <= 0:
            return
        fh, fw = bgr_frame.shape[:2]
        disp_w, disp_h = overlay_source_dims(fw, fh, rotation_deg)
        cam_w, cam_h, _, _ = camera_overlay_pixels(
            encode_w,
            encode_h,
            size_key,
            "bottom_right",
            ox=cam_ox,
            oy=cam_oy,
            frame_w=disp_w,
            frame_h=disp_h,
            zoom=zoom,
            rotation_deg=rotation_deg,
        )
        sw = max(32, int(self.box_w * cam_w / encode_w))
        sh = max(32, int(self.box_h * cam_h / encode_h))
        sx = max(0, min(int(self.box_w * cam_ox / encode_w), self.box_w - sw))
        sy = max(0, min(int(self.box_h * cam_oy / encode_h), self.box_h - sh))
        patch = prepare_webcam_patch(bgr_frame, sw, sh, "square", zoom, rotation_deg)
        rgb = cv2.cvtColor(patch, cv2.COLOR_BGR2RGB)
        if shape == "circle":
            mask = np.zeros((sh, sw), dtype=np.float32)
            radius = max(1, min(sw, sh) // 2 - 1)
            cv2.circle(mask, (sw // 2, sh // 2), radius, 1.0, -1)
            key = np.array([1, 1, 1], dtype=np.float32)
            rgb = (
                rgb.astype(np.float32) * mask[:, :, None]
                + key * (1.0 - mask[:, :, None])
            ).astype(np.uint8)
        img = Image.fromarray(rgb)
        self._webcam_photo = ImageTk.PhotoImage(img)
        self.canvas.delete("webcam")
        self.canvas.create_image(sx, sy, anchor="nw", image=self._webcam_photo, tags="webcam")
        self.canvas.tag_raise("webcam")

    def follow_center(self, cx: int, cy: int) -> tuple[int, int]:
        """Place the box around (cx, cy), keeping the near screen edges stuck on-screen."""
        rect = monitor_rect_at(cx, cy)
        x, y = pin_box_top_left(cx, cy, self.box_w, self.box_h, rect)
        self.move_top_left(x, y)
        return x + (self.box_w // 2), y + (self.box_h // 2)

    def center_on_screen(self) -> None:
        cx, cy = get_cursor_pos()
        left, top, right, bottom = monitor_rect_at(cx, cy)
        x = (left + right - self.box_w) // 2
        y = (top + bottom - self.box_h) // 2
        x, y = clamp_top_left(x, y, self.box_w, self.box_h, (left, top, right, bottom))
        self.move_top_left(x, y)

    def lift_behind(self, other: tk.Misc) -> None:
        """Keep the setup dialog visually above this preview."""
        try:
            other.attributes("-topmost", True)
            other.lift()
        except tk.TclError:
            pass

    def set_click_through(self, through: bool) -> None:
        """When False, the overlay receives mouse clicks (drag camera while parked)."""
        if not self.hwnd:
            return
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        if through:
            style |= WS_EX_TRANSPARENT
        else:
            style &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style)

    def webcam_screen_rect(
        self,
        encode_w: int,
        encode_h: int,
        cam_ox: int,
        cam_oy: int,
        size_key: str,
        frame_w: int | None,
        frame_h: int | None,
        zoom: float,
        rotation_deg: int,
    ) -> tuple[int, int, int, int]:
        if encode_w <= 0 or encode_h <= 0:
            return 0, 0, 0, 0
        cam_w, cam_h, _, _ = camera_overlay_pixels(
            encode_w,
            encode_h,
            size_key,
            "bottom_right",
            ox=cam_ox,
            oy=cam_oy,
            frame_w=frame_w,
            frame_h=frame_h,
            zoom=zoom,
            rotation_deg=rotation_deg,
        )
        sw = max(1, int(self.box_w * cam_w / encode_w))
        sh = max(1, int(self.box_h * cam_h / encode_h))
        sx = max(0, min(int(self.box_w * cam_ox / encode_w), self.box_w - sw))
        sy = max(0, min(int(self.box_h * cam_oy / encode_h), self.box_h - sh))
        return sx, sy, sw, sh

    def destroy(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:
            pass


def setup_layered_window(hwnd: int) -> None:
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    user32.SetLayeredWindowAttributes(hwnd, KEY_COLORREF, 0, LWA_COLORKEY)
    # Keep the viewfinder out of the recording when Windows supports it
    try:
        user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    except OSError:
        pass


def setup_hud_window(hwnd: int) -> None:
    """Control bar: visible, clickable, and excluded from the recording."""
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_TOOLWINDOW
    style &= ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    try:
        user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    except OSError:
        pass


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class RecordHud:
    """Bottom-left red rec dot. Timer always visible; buttons appear on hover."""

    def __init__(self, master: tk.Misc, on_start, on_stop, on_refresh, on_exit) -> None:
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_refresh = on_refresh
        self._on_exit = on_exit
        self._busy = False
        self._after_ids: list[str] = []
        self._hovered = False
        self._recording = False

        self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="#141414")
        self.hwnd = 0
        self.last_geom: tuple[int, int, int, int] | None = None

        self.wrap = tk.Frame(self.win, bg="#141414", padx=8, pady=8)
        self.wrap.pack()

        self.dot = tk.Canvas(
            self.wrap, width=22, height=22, bg="#141414", highlightthickness=0, bd=0
        )
        self.dot.pack()
        self._dot_id = self.dot.create_oval(3, 3, 19, 19, fill="#FF2B2B", outline="#FF2B2B")

        self.timer = tk.Label(
            self.wrap,
            text="00:00",
            fg="#FF2B2B",
            bg="#141414",
            font=("Segoe UI", 10, "bold"),
        )
        self.timer.pack(pady=(4, 0))

        self.btns = tk.Frame(self.wrap, bg="#141414")
        btn_style = {
            "font": ("Segoe UI", 9, "bold"),
            "bd": 0,
            "padx": 10,
            "pady": 3,
            "cursor": "hand2",
            "width": 8,
        }
        self.btn_start = tk.Button(
            self.btns, text="Start", bg="#1F7A3A", fg="white", activebackground="#25964A",
            command=self._click_start, **btn_style,
        )
        self.btn_stop = tk.Button(
            self.btns, text="Stop", bg="#A31D1D", fg="white", activebackground="#C42323",
            command=self._click_stop, **btn_style,
        )
        self.btn_refresh = tk.Button(
            self.btns, text="Refresh", bg="#3A3A3A", fg="white", activebackground="#555555",
            command=self._click_refresh, **btn_style,
        )
        self.btn_exit = tk.Button(
            self.btns, text="Exit", bg="#5A1A1A", fg="white", activebackground="#7A2424",
            command=self._on_exit, **btn_style,
        )
        for widget in (self.btn_start, self.btn_stop, self.btn_refresh, self.btn_exit):
            widget.pack(pady=2)

        self.count_win = tk.Toplevel(master)
        self.count_win.overrideredirect(True)
        self.count_win.attributes("-topmost", True)
        self.count_win.configure(bg="#141414")
        self.count_label = tk.Label(
            self.count_win,
            text="",
            fg="#FF2B2B",
            bg="#141414",
            font=("Segoe UI", 140, "bold"),
            padx=40,
            pady=10,
        )
        self.count_label.pack()
        self.count_win.withdraw()

        self.win.bind("<Enter>", self._on_enter)
        self.win.bind("<Leave>", self._on_leave)
        for child in self.wrap.winfo_children():
            child.bind("<Enter>", self._on_enter)
            child.bind("<Leave>", self._on_leave)
        for child in self.btns.winfo_children():
            child.bind("<Enter>", self._on_enter)
            child.bind("<Leave>", self._on_leave)

        self.win.update_idletasks()
        self.win.update()
        self.hwnd = get_hwnd(self.win)
        setup_hud_window(self.hwnd)
        self.count_hwnd = get_hwnd(self.count_win)
        setup_hud_window(self.count_hwnd)
        self.set_recording(False, 0.0)
        self._show_buttons(False)
        self.place_bottom_left()

    def _click_start(self) -> None:
        if self._busy or self._recording:
            return
        self.countdown(self._on_start)

    def _click_stop(self) -> None:
        self._cancel_countdown()
        if not self._recording:
            return
        self._on_stop()

    def _click_refresh(self) -> None:
        self._cancel_countdown()
        if self._recording:
            self._on_stop()
        self.countdown(self._on_start)

    def countdown(self, callback) -> None:
        if self._busy:
            return
        self._busy = True
        self._cancel_after()
        self._pending = callback
        self._n = 3
        self._step_countdown()

    def _cancel_countdown(self) -> None:
        self._cancel_after()
        self._hide_count()
        self._busy = False
        self._pending = None

    def _step_countdown(self) -> None:
        if self._n >= 1:
            self._show_count(self._n)
            self._n -= 1
            self._after_ids.append(self.win.after(1000, self._step_countdown))
            return
        self._hide_count()
        self._busy = False
        cb = getattr(self, "_pending", None)
        self._pending = None
        if cb is not None:
            cb()

    def _show_count(self, n: int) -> None:
        self.count_label.config(text=str(n))
        self.count_win.update_idletasks()
        cx, cy = get_cursor_pos()
        left, top, right, bottom = monitor_rect_at(cx, cy)
        self.count_win.deiconify()
        self.count_win.update_idletasks()
        w = max(80, int(self.count_win.winfo_reqwidth()))
        h = max(80, int(self.count_win.winfo_reqheight()))
        x = left + (right - left - w) // 2
        y = top + (bottom - top - h) // 2
        user32.SetWindowPos(
            self.count_hwnd,
            HWND_TOPMOST,
            x,
            y,
            w,
            h,
            SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )

    def _hide_count(self) -> None:
        try:
            self.count_win.withdraw()
        except tk.TclError:
            pass

    def _cancel_after(self) -> None:
        for aid in self._after_ids:
            try:
                self.win.after_cancel(aid)
            except Exception:
                pass
        self._after_ids.clear()

    def _on_enter(self, _event=None) -> None:
        self._hovered = True
        self._show_buttons(True)

    def _on_leave(self, _event=None) -> None:
        self.win.after(120, self._hide_if_left)

    def _hide_if_left(self) -> None:
        if self._pointer_inside():
            return
        self._hovered = False
        self._show_buttons(False)

    def _pointer_inside(self) -> bool:
        try:
            x, y = get_cursor_pos()
            wx = int(self.win.winfo_rootx())
            wy = int(self.win.winfo_rooty())
            ww = int(self.win.winfo_width())
            wh = int(self.win.winfo_height())
        except tk.TclError:
            return False
        return wx <= x <= wx + ww and wy <= y <= wy + wh

    def _show_buttons(self, show: bool) -> None:
        if show:
            if not self.btns.winfo_ismapped():
                self.btns.pack(pady=(8, 0))
        else:
            if self.btns.winfo_ismapped():
                self.btns.pack_forget()
        self.place_bottom_left()

    def set_recording(self, recording: bool, elapsed_s: float) -> None:
        self._recording = recording
        self.timer.config(text=_format_elapsed(elapsed_s))
        fill = "#FF2B2B" if recording else "#7A2A2A"
        self.dot.itemconfig(self._dot_id, fill=fill, outline=fill)
        if recording:
            self.btn_start.config(state="disabled")
            self.btn_stop.config(state="normal")
        else:
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")

    def place_bottom_left(self, ax: int | None = None, ay: int | None = None) -> None:
        try:
            self.win.update_idletasks()
        except tk.TclError:
            return
        hud_w = max(1, int(self.win.winfo_reqwidth()))
        hud_h = max(1, int(self.win.winfo_reqheight()))
        if ax is None or ay is None:
            ax, ay = get_cursor_pos()
        left, top, right, bottom = monitor_work_rect_at(ax, ay)
        margin = 8
        x = left + margin
        y = bottom - hud_h - margin
        x = max(left, min(x, right - hud_w))
        y = max(top, min(y, bottom - hud_h))
        key = (x, y, hud_w, hud_h)
        if self.last_geom == key:
            return
        self.last_geom = key
        if self.hwnd:
            user32.SetWindowPos(
                self.hwnd,
                HWND_TOPMOST,
                x,
                y,
                hud_w,
                hud_h,
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        else:
            self.win.geometry(f"{hud_w}x{hud_h}+{x}+{y}")

    def destroy(self) -> None:
        self._cancel_after()
        self._hide_count()
        try:
            self.count_win.destroy()
        except tk.TclError:
            pass
        try:
            self.win.destroy()
        except tk.TclError:
            pass


class WebcamPreview:
    """Live webcam preview on the capture box (excluded from recording)."""

    def __init__(
        self,
        master: tk.Misc,
        shape: str,
        size_key: str,
        position_key: str,
        *,
        draggable: bool = False,
        encode_ox: int | None = None,
        encode_oy: int | None = None,
        on_moved=None,
    ) -> None:
        self.shape = shape if shape in CAMERA_SHAPES else "circle"
        self.size_key = size_key if size_key in CAMERA_SIZES else "medium"
        self.position_key = (
            position_key if position_key in CAMERA_POSITIONS else "bottom_right"
        )
        self.encode_ox = encode_ox
        self.encode_oy = encode_oy
        self.draggable = draggable
        self._on_moved = on_moved
        self._drag: dict[str, int] | None = None
        self._box: tuple[int, int, int, int, int, int] | None = None
        self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="#1a1a1a")
        self.canvas = tk.Canvas(
            self.win, bg="#1a1a1a", highlightthickness=0, bd=0
        )
        self.canvas.pack()
        self.hwnd = 0
        self._photo = None
        self._last_geom: tuple[int, int, int, int] | None = None
        if draggable:
            self.canvas.configure(cursor="hand2")
            self.canvas.bind("<ButtonPress-1>", self._drag_start)
            self.canvas.bind("<B1-Motion>", self._drag_motion)
            self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.win.update_idletasks()
        self.win.update()
        self.hwnd = get_hwnd(self.win)
        setup_hud_window(self.hwnd)

    def set_options(
        self,
        shape: str,
        size_key: str,
        position_key: str,
        encode_ox: int | None = None,
        encode_oy: int | None = None,
    ) -> None:
        self.shape = shape if shape in CAMERA_SHAPES else self.shape
        self.size_key = size_key if size_key in CAMERA_SIZES else self.size_key
        self.position_key = (
            position_key if position_key in CAMERA_POSITIONS else self.position_key
        )
        if encode_ox is not None and encode_oy is not None:
            self.encode_ox = int(encode_ox)
            self.encode_oy = int(encode_oy)
        self._last_geom = None

    def place_on_box(
        self,
        box_x: int,
        box_y: int,
        box_w: int,
        box_h: int,
        encode_w: int,
        encode_h: int,
    ) -> None:
        self._box = (box_x, box_y, box_w, box_h, encode_w, encode_h)
        x, y, w, h = screen_camera_preview_rect(
            box_x,
            box_y,
            box_w,
            box_h,
            encode_w,
            encode_h,
            self.size_key,
            self.position_key,
            ox=self.encode_ox,
            oy=self.encode_oy,
        )
        key = (x, y, w, h)
        if self._last_geom == key:
            return
        self._last_geom = key
        self.canvas.config(width=w, height=h)
        if self.hwnd:
            user32.SetWindowPos(
                self.hwnd,
                HWND_TOPMOST,
                x,
                y,
                w,
                h,
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        else:
            self.win.geometry(f"{w}x{h}+{x}+{y}")

    def lift_above(self, other_win: tk.Misc) -> None:
        try:
            other_hwnd = get_hwnd(other_win)
            user32.SetWindowPos(
                self.hwnd,
                other_hwnd,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        except Exception:
            pass

    def _drag_start(self, event: tk.Event) -> None:
        self._drag = {"x": int(event.x_root), "y": int(event.y_root)}

    def _drag_motion(self, event: tk.Event) -> None:
        if self._drag is None or self._box is None:
            return
        dx = int(event.x_root) - self._drag["x"]
        dy = int(event.y_root) - self._drag["y"]
        self._drag["x"] = int(event.x_root)
        self._drag["y"] = int(event.y_root)
        try:
            nx = int(self.win.winfo_x()) + dx
            ny = int(self.win.winfo_y()) + dy
        except tk.TclError:
            return
        box_x, box_y, box_w, box_h, enc_w, enc_h = self._box
        cam_w, cam_h, _, _ = camera_overlay_pixels(
            enc_w, enc_h, self.size_key, self.position_key
        )
        screen_w = max(48, int(box_w * cam_w / enc_w))
        screen_h = max(48, int(box_h * cam_h / enc_h))
        min_x = box_x
        min_y = box_y
        max_x = box_x + box_w - screen_w
        max_y = box_y + box_h - screen_h
        nx = max(min_x, min(nx, max_x))
        ny = max(min_y, min(ny, max_y))
        self._last_geom = None
        if self.hwnd:
            user32.SetWindowPos(
                self.hwnd,
                HWND_TOPMOST,
                nx,
                ny,
                screen_w,
                screen_h,
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        else:
            self.win.geometry(f"{screen_w}x{screen_h}+{nx}+{ny}")

    def _drag_end(self, _event: tk.Event) -> None:
        self._drag = None
        if self._box is None:
            return
        box_x, box_y, box_w, box_h, enc_w, enc_h = self._box
        try:
            sx = int(self.win.winfo_x())
            sy = int(self.win.winfo_y())
        except tk.TclError:
            return
        cam_w, cam_h, _, _ = camera_overlay_pixels(
            enc_w, enc_h, self.size_key, self.position_key
        )
        ox = int(round((sx - box_x) * enc_w / box_w))
        oy = int(round((sy - box_y) * enc_h / box_h))
        ox = max(0, min(ox, enc_w - cam_w))
        oy = max(0, min(oy, enc_h - cam_h))
        self.encode_ox = ox
        self.encode_oy = oy
        if self._on_moved is not None:
            self._on_moved(ox, oy)

    def refresh(self, webcam: WebcamCapture | None) -> None:
        if webcam is None:
            return
        import cv2
        from PIL import Image, ImageDraw, ImageTk

        frame = webcam.get_frame()
        if frame is None:
            return
        try:
            w = max(8, int(self.canvas.winfo_width()))
            h = max(8, int(self.canvas.winfo_height()))
        except tk.TclError:
            return
        if w < 8 or h < 8:
            return
        small = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        if self.shape == "circle":
            mask = Image.new("L", (w, h), 0)
            draw = ImageDraw.Draw(mask)
            draw.ellipse((0, 0, w - 1, h - 1), fill=255)
            img = img.convert("RGBA")
            img.putalpha(mask)
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

    def destroy(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record the screen inside a cursor-centered aspect-ratio border"
    )
    parser.add_argument(
        "ratio",
        nargs="?",
        choices=("16:9", "9:16"),
        help="Skip the size picker and use this aspect ratio",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=None,
        help="Custom frame width in pixels",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=None,
        help="Custom frame height in pixels",
    )
    parser.add_argument(
        "--border",
        type=int,
        default=BORDER_WIDTH,
        help=f"Border line thickness in pixels (default {BORDER_WIDTH})",
    )
    parser.add_argument(
        "--quality",
        choices=tuple(QUALITY_PRESETS),
        default=None,
        help="Video quality preset: low, hd, 2k, 4k",
    )
    parser.add_argument(
        "--fps",
        type=int,
        choices=FPS_CHOICES,
        default=None,
        help="Recording frame rate: 24, 30, or 60",
    )
    parser.add_argument(
        "--mic",
        default=None,
        help="Microphone name (DirectShow). Use 'none' for no audio.",
    )
    parser.add_argument(
        "--overlay-only",
        action="store_true",
        help="Show the following border without recording",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Use console prompts instead of the setup window",
    )
    return parser.parse_args()


# -----------------------------------------------------------------------------
# Setup UI
# -----------------------------------------------------------------------------


def _mic_choices(mics: list[str]) -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = []
    default = preferred_microphone(mics)
    if default:
        choices.append((f"Auto-detected: {default}", default))
        for name in mics:
            if name != default:
                choices.append((name, name))
    choices.append(("No audio (screen only)", NO_AUDIO))
    return choices


def _camera_choices(
    catalog: list[CameraDevice],
    working: set[str] | None = None,
) -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = []
    names = [dev.name for dev in catalog]
    default = preferred_camera(names) if names else None
    for dev in catalog:
        name = dev.name
        if dev.backend == "http" and dev.source_url:
            status = "ready" if working is not None and name in working else "start DroidCam client"
        elif not dev.driver_ok:
            status = f"driver {dev.pnp_status.lower()} — fix in Device Manager"
        elif working is not None:
            status = "ready" if name in working else "select to connect"
        else:
            status = ""
        label = f"{name}  ({status})" if status else name
        if name == default:
            label = f"{label}  — default"
        choices.append((label, name))
    choices.append(("No webcam overlay", NO_CAMERA))
    return choices


def open_shortcuts_settings(
    parent: tk.Misc,
    shortcut_vars: dict[str, tk.StringVar],
) -> None:
    """Modal editor for keyboard shortcuts."""
    dialog = tk.Toplevel(parent)
    dialog.title("Keyboard shortcuts")
    dialog.configure(bg=SETUP_PANEL)
    dialog.transient(parent)
    dialog.grab_set()
    dialog.resizable(False, False)
    pad = {"padx": 20, "pady": 8}
    tk.Label(
        dialog,
        text="Keyboard shortcuts",
        font=("Segoe UI", 14, "bold"),
        fg=SETUP_TEXT,
        bg=SETUP_PANEL,
    ).pack(anchor="w", **pad)
    tk.Label(
        dialog,
        text="Examples: Ctrl+Caps, Ctrl+Shift+Right, Ctrl+Numpad7, Ctrl+0",
        font=("Segoe UI", 9),
        fg=SETUP_MUTED,
        bg=SETUP_PANEL,
    ).pack(anchor="w", padx=20, pady=(0, 12))
    body = tk.Frame(dialog, bg=SETUP_PANEL)
    body.pack(fill="both", expand=True, padx=16)
    for key in DEFAULT_SHORTCUTS:
        row = tk.Frame(body, bg=SETUP_PANEL)
        row.pack(fill="x", pady=4)
        tk.Label(
            row,
            text=SHORTCUT_LABELS[key],
            font=("Segoe UI", 10),
            fg=SETUP_TEXT,
            bg=SETUP_PANEL,
            width=32,
            anchor="w",
        ).pack(side="left")
        ttk.Entry(row, textvariable=shortcut_vars[key], width=24).pack(side="left", padx=(8, 0))
    tk.Button(
        dialog,
        text="Done",
        font=("Segoe UI", 10, "bold"),
        fg=SETUP_BG,
        bg=SETUP_ACCENT,
        relief="flat",
        padx=20,
        pady=8,
        cursor="hand2",
        command=dialog.destroy,
    ).pack(pady=16)
    dialog.update_idletasks()
    w, h = dialog.winfo_width(), dialog.winfo_height()
    px = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
    py = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
    dialog.geometry(f"+{max(0, px)}+{max(0, py)}")


def _quality_combo_items() -> list[tuple[str, str]]:
    return [(f"{p['label']} — {p['detail']}", key) for key, p in QUALITY_PRESETS.items()]


def _fps_combo_items() -> list[tuple[str, int]]:
    items: list[tuple[str, int]] = []
    for fps in FPS_CHOICES:
        label = f"{fps} fps"
        if fps == 30:
            label += " (recommended)"
        items.append((label, fps))
    return items


def _shape_combo_items() -> list[tuple[str, str]]:
    return [("Square (full frame)", "square"), ("Circle", "circle")]


def _size_combo_items() -> list[tuple[str, str]]:
    return [(CAMERA_SIZE_LABELS[k], k) for k in CAMERA_SIZES]


def _position_combo_items() -> list[tuple[str, str]]:
    return [(CAMERA_POSITION_LABELS[k], k) for k in CAMERA_POSITIONS]


def _zoom_combo_items() -> list[tuple[str, float]]:
    return [
        ("0.5× zoom out", 0.5),
        ("1.0× normal", 1.0),
        ("1.25×", 1.25),
        ("1.5×", 1.5),
        ("2.0×", 2.0),
        ("2.5×", 2.5),
        ("3.0× close-up", 3.0),
    ]


def _rotation_combo_items() -> list[tuple[str, int]]:
    return [
        ("0° normal", 0),
        ("90° right", 90),
        ("180° upside down", 180),
        ("270° left", 270),
    ]


def _ratio_combo_items(quality_key: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for ratio_key in ("16:9", "9:16"):
        enc = size_for_quality(ratio_key, quality_key)
        w, h = screen_fit(*enc)
        fit = "" if (w, h) == enc else " (fits screen)"
        label = "16:9 widescreen" if ratio_key == "16:9" else "9:16 vertical"
        items.append((f"{label} — {w}×{h}{fit}", ratio_key))
    items.append(("Custom width × height", "custom"))
    return items


def ask_setup_gui(
    mics: list[str],
    catalog: list[CameraDevice],
    working_names: set[str] | None = None,
) -> dict | None:
    """Quality / microphone / frame-size window. None if the user cancels."""
    result: dict | None = None
    root = tk.Tk()
    root.title("Cursor Follower — Record")
    configure_setup_theme(root)
    setup_fullscreen_root(root)

    quality_items = _quality_combo_items()
    quality_by_label = {lbl: key for lbl, key in quality_items}
    default_quality = next(
        lbl for lbl, key in quality_items if key == "hd"
    )
    quality_display = tk.StringVar(value=default_quality)

    fps_items = _fps_combo_items()
    fps_by_label = {lbl: fps for lbl, fps in fps_items}
    default_fps = next(lbl for lbl, fps in fps_items if fps == 30)
    fps_display = tk.StringVar(value=default_fps)

    ratio_by_label: dict[str, str] = {}
    ratio_display = tk.StringVar()

    custom_w = tk.StringVar(value="1920")
    custom_h = tk.StringVar(value="1080")

    mic_choices = _mic_choices(mics)
    mic_by_label = {lbl: val for lbl, val in mic_choices}
    mic_display = tk.StringVar(value=mic_choices[0][0])

    camera_choices = _camera_choices(catalog, working_names)
    camera_by_label = {lbl: val for lbl, val in camera_choices}
    camera_display = tk.StringVar(value=camera_choices[0][0])

    shape_items = _shape_combo_items()
    shape_by_label = {lbl: key for lbl, key in shape_items}
    shape_display = tk.StringVar(value=shape_items[0][0])

    size_items = _size_combo_items()
    size_by_label = {lbl: key for lbl, key in size_items}
    size_display = tk.StringVar(value=size_items[1][0])

    position_items = _position_combo_items()
    position_by_label = {lbl: key for lbl, key in position_items}
    position_display = tk.StringVar(value=position_items[2][0])

    zoom_items = _zoom_combo_items()
    zoom_by_label = {lbl: val for lbl, val in zoom_items}
    zoom_display = tk.StringVar(value=zoom_items[1][0])

    rotation_items = _rotation_combo_items()
    rotation_by_label = {lbl: val for lbl, val in rotation_items}
    rotation_display = tk.StringVar(value=rotation_items[0][0])

    shortcut_vars = {
        key: tk.StringVar(value=DEFAULT_SHORTCUTS[key]) for key in DEFAULT_SHORTCUTS
    }
    size_note = tk.StringVar()
    preview_holder: dict[str, CyanBorder | None] = {"ov": None}
    cam_state: dict = {
        "webcam": None,
        "encode_ox": 0,
        "encode_oy": 0,
        "use_custom": False,
        "after_id": None,
    }

    def stop_setup_webcam(keep_capture: bool = False) -> WebcamCapture | None:
        aid = cam_state.get("after_id")
        if aid:
            try:
                root.after_cancel(aid)
            except Exception:
                pass
            cam_state["after_id"] = None
        wc = cam_state.get("webcam")
        if wc is not None:
            if keep_capture:
                cam_state["webcam"] = None
                return wc
            wc.stop()
            cam_state["webcam"] = None
        return None

    def webcam_frame_dims() -> tuple[int | None, int | None]:
        wc = cam_state.get("webcam")
        if wc is None:
            return None, None
        frame = wc.get_frame()
        if frame is None:
            return None, None
        return int(frame.shape[1]), int(frame.shape[0])

    def apply_preset_position() -> None:
        enc_w, enc_h = current_encode_size()
        fw, fh = webcam_frame_dims()
        rot = camera_rotation_value()
        disp_w, disp_h = (
            overlay_source_dims(fw, fh, rot) if fw and fh else (fw, fh)
        )
        _, _, ox, oy = camera_overlay_pixels(
            enc_w,
            enc_h,
            camera_size_key(),
            camera_position_key(),
            frame_w=disp_w,
            frame_h=disp_h,
            zoom=camera_zoom_value(),
            rotation_deg=rot,
        )
        cam_state["encode_ox"] = ox
        cam_state["encode_oy"] = oy
        cam_state["use_custom"] = False

    def on_camera_moved(ox: int, oy: int) -> None:
        cam_state["encode_ox"] = ox
        cam_state["encode_oy"] = oy
        cam_state["use_custom"] = True

    def update_preview() -> None:
        w, h = current_size_for(ratio_key())
        if w < 64 or h < 64:
            return
        ov = preview_holder.get("ov")
        if ov is None:
            return
        if ov.box_w != w or ov.box_h != h:
            ov.set_size(w, h)
        ov.center_on_screen()
        lift_preview_above_settings(root, ov)

    def refresh_camera_overlay() -> None:
        wc = cam_state.get("webcam")
        ov = preview_holder.get("ov")
        if wc is None or ov is None:
            return
        frame = wc.get_frame()
        enc_w, enc_h = current_encode_size()
        if frame is None or enc_w <= 0 or enc_h <= 0:
            return
        ov.update_webcam_overlay(
            frame,
            enc_w,
            enc_h,
            int(cam_state.get("encode_ox", 0)),
            int(cam_state.get("encode_oy", 0)),
            camera_shape_key(),
            camera_size_key(),
            camera_zoom_value(),
            camera_rotation_value(),
        )

    def tick_setup_camera() -> None:
        refresh_camera_overlay()
        cam_state["after_id"] = root.after(33, tick_setup_camera)

    def sync_setup_webcam() -> None:
        stop_setup_webcam()
        ov = preview_holder.get("ov")
        if ov is not None:
            ov.clear_webcam_overlay()
        name = selected_camera_name()
        if not name:
            if not cam_state.get("use_custom"):
                apply_preset_position()
            update_preview()
            return
        dev = find_camera(name, catalog)
        if dev is None:
            return
        try:
            wc = WebcamCapture.open_device(dev, catalog=catalog)
        except Exception:
            return
        cam_state["webcam"] = wc
        if not cam_state.get("use_custom"):
            apply_preset_position()
        update_preview()
        tick_setup_camera()

    def on_camera_combo_change(_event: object = None) -> None:
        cam_state["use_custom"] = False
        sync_setup_webcam()

    def on_camera_layout_change(_event: object = None) -> None:
        if not cam_state.get("use_custom"):
            apply_preset_position()
        update_preview()
        refresh_camera_overlay()

    def close_setup() -> None:
        stop_setup_webcam()
        ov = preview_holder.get("ov")
        if ov is not None:
            try:
                ov.destroy()
            except Exception:
                pass
        root.destroy()

    def selected_camera_name() -> str | None:
        name = camera_by_label.get(camera_display.get(), NO_CAMERA)
        if name == NO_CAMERA:
            return None
        return name

    def camera_shape_key() -> str:
        return shape_by_label.get(shape_display.get(), "square")

    def camera_size_key() -> str:
        return size_by_label.get(size_display.get(), "medium")

    def camera_position_key() -> str:
        return position_by_label.get(position_display.get(), "bottom_left")

    def camera_zoom_value() -> float:
        return zoom_by_label.get(zoom_display.get(), 1.0)

    def camera_rotation_value() -> int:
        return rotation_by_label.get(rotation_display.get(), 0)

    def quality_key() -> str:
        return quality_by_label.get(quality_display.get(), "hd")

    def ratio_key() -> str:
        return ratio_by_label.get(ratio_display.get(), "16:9")

    def fps_value() -> int:
        return fps_by_label.get(fps_display.get(), 30)

    def current_size_for(ratio: str) -> tuple[int, int]:
        q = quality_key()
        if ratio == "custom":
            try:
                raw_w, raw_h = even(int(custom_w.get())), even(int(custom_h.get()))
            except ValueError:
                return 0, 0
            if raw_w < 64 or raw_h < 64:
                return raw_w, raw_h
            return screen_fit(raw_w, raw_h)
        return screen_fit(*size_for_quality(ratio, q))

    def current_encode_size() -> tuple[int, int]:
        chosen = ratio_key()
        if chosen == "custom":
            try:
                return even(int(custom_w.get())), even(int(custom_h.get()))
            except ValueError:
                w, h = current_size_for("custom")
                return max(w, 64), max(h, 64)
        return size_for_quality(chosen, quality_key())

    def refresh_ratio_dropdown(keep_key: str | None = None) -> None:
        items = _ratio_combo_items(quality_key())
        labels = []
        ratio_by_label.clear()
        for lbl, key in items:
            labels.append(lbl)
            ratio_by_label[lbl] = key
        ratio_combo.configure(values=labels)
        want = keep_key or ratio_key()
        pick = next((lbl for lbl, key in items if key == want), items[0][0])
        ratio_display.set(pick)

    def update_custom_state() -> None:
        state = "normal" if ratio_key() == "custom" else "disabled"
        entry_w.configure(state=state)
        entry_h.configure(state=state)

    def update_size_note() -> None:
        chosen = ratio_key()
        w_now, h_now = current_size_for(chosen)
        if w_now < 64 or h_now < 64:
            size_note.set("Enter width and height (at least 64 × 64).")
            return
        enc_w, enc_h = current_encode_size()
        q = QUALITY_PRESETS[quality_key()]
        extra = (
            f"Saved as {enc_w} × {enc_h}."
            if (enc_w, enc_h) != (w_now, h_now)
            else ""
        )
        size_note.set(
            f"On screen: {w_now} × {h_now} px. {extra} "
            f"{fps_value()} fps ({q['label']})."
        )

    def on_settings_change(*_args: object) -> None:
        update_custom_state()
        update_size_note()
        if not cam_state.get("use_custom"):
            apply_preset_position()
        update_preview()
        refresh_camera_overlay()

    def on_quality_change(_event: object = None) -> None:
        refresh_ratio_dropdown()
        on_settings_change()

    def start() -> None:
        nonlocal result
        w, h = current_size_for(ratio_key())
        if w < 64 or h < 64:
            size_note.set("Frame must be at least 64 × 64 pixels.")
            return
        enc_w, enc_h = current_encode_size()
        cam_name = selected_camera_name()
        if cam_state.get("use_custom"):
            ox, oy = int(cam_state["encode_ox"]), int(cam_state["encode_oy"])
        else:
            _, _, ox, oy = camera_overlay_pixels(
                enc_w,
                enc_h,
                camera_size_key(),
                camera_position_key(),
            )
        shortcuts = {
            key: shortcut_vars[key].get().strip() for key in DEFAULT_SHORTCUTS
        }
        result = {
            "quality": quality_key(),
            "fps": fps_value(),
            "ratio": ratio_key(),
            "width": w,
            "height": h,
            "encode_width": enc_w,
            "encode_height": enc_h,
            "mic_name": mic_by_label.get(mic_display.get(), mic_choices[0][1]),
            "camera_name": cam_name,
            "camera_shape": camera_shape_key(),
            "camera_size": camera_size_key(),
            "camera_position": camera_position_key(),
            "camera_ox": ox,
            "camera_oy": oy,
            "camera_zoom": camera_zoom_value(),
            "camera_rotation": camera_rotation_value(),
            "shortcuts": shortcuts,
            "webcam_capture": stop_setup_webcam(keep_capture=True),
            "record": True,
        }
        ov = preview_holder.get("ov")
        if ov is not None:
            try:
                ov.destroy()
            except Exception:
                pass
        root.destroy()

    outer = tk.Frame(root, bg=SETUP_BG)
    outer.pack(fill="both", expand=True)

    header = tk.Frame(outer, bg=SETUP_BG, padx=48, pady=36)
    header.pack(fill="x")
    tk.Label(
        header,
        text="Cursor Follower",
        font=("Segoe UI", 34, "bold"),
        fg=SETUP_ACCENT,
        bg=SETUP_BG,
    ).pack(anchor="w")
    tk.Label(
        header,
        text="Recording setup — choose capture and camera options below",
        font=("Segoe UI", 15),
        fg=SETUP_MUTED,
        bg=SETUP_BG,
    ).pack(anchor="w", pady=(8, 0))

    columns = tk.Frame(outer, bg=SETUP_BG)
    columns.pack(fill="both", expand=True, padx=40, pady=(0, 12))

    col_left = tk.Frame(columns, bg=SETUP_PANEL, padx=40, pady=32)
    col_left.pack(side="left", fill="both", expand=True, padx=(0, 8))

    col_right = tk.Frame(columns, bg=SETUP_PANEL, padx=40, pady=32)
    col_right.pack(side="left", fill="both", expand=True, padx=(8, 0))

    ttk.Label(col_left, text="Capture settings", style="SetupColTitle.TLabel").pack(
        anchor="w", pady=(0, 24)
    )
    ttk.Label(col_right, text="Camera overlay", style="SetupColTitle.TLabel").pack(
        anchor="w", pady=(0, 24)
    )

    form_left = tk.Frame(col_left, bg=SETUP_PANEL)
    form_left.pack(fill="x")
    form_left.columnconfigure(1, weight=1)

    form_right = tk.Frame(col_right, bg=SETUP_PANEL)
    form_right.pack(fill="x")
    form_right.columnconfigure(1, weight=1)

    def add_row(
        form: tk.Frame, row: int, label: str, widget: tk.Widget, pady: int = 12
    ) -> None:
        ttk.Label(form, text=label, style="Setup.TLabel").grid(
            row=row, column=0, sticky="w", pady=pady, padx=(0, 20)
        )
        widget.grid(row=row, column=1, sticky="ew", pady=pady)

    combo_width = 42

    quality_combo = ttk.Combobox(
        form_left,
        textvariable=quality_display,
        values=[lbl for lbl, _ in quality_items],
        state="readonly",
        width=combo_width,
    )
    add_row(form_left, 0, "Video quality", quality_combo)

    fps_combo = ttk.Combobox(
        form_left,
        textvariable=fps_display,
        values=[lbl for lbl, _ in fps_items],
        state="readonly",
        width=combo_width,
    )
    add_row(form_left, 1, "Frame rate", fps_combo)

    mic_combo = ttk.Combobox(
        form_left,
        textvariable=mic_display,
        values=[lbl for lbl, _ in mic_choices],
        state="readonly",
        width=combo_width,
    )
    add_row(form_left, 2, "Microphone", mic_combo)

    camera_combo = ttk.Combobox(
        form_left,
        textvariable=camera_display,
        values=[lbl for lbl, _ in camera_choices],
        state="readonly",
        width=combo_width,
    )
    add_row(form_left, 3, "Web camera", camera_combo)

    ratio_combo = ttk.Combobox(
        form_left,
        textvariable=ratio_display,
        state="readonly",
        width=combo_width,
    )
    add_row(form_left, 4, "Frame size", ratio_combo)

    custom_row = tk.Frame(form_left, bg=SETUP_PANEL)
    custom_row.grid(row=5, column=1, sticky="w", pady=(0, 8))
    ttk.Label(custom_row, text="Width", style="Setup.TLabel").pack(side="left")
    entry_w = ttk.Entry(custom_row, textvariable=custom_w, width=10)
    entry_w.pack(side="left", padx=(8, 20))
    ttk.Label(custom_row, text="Height", style="Setup.TLabel").pack(side="left")
    entry_h = ttk.Entry(custom_row, textvariable=custom_h, width=10)
    entry_h.pack(side="left", padx=(8, 0))

    tk.Label(
        form_left,
        textvariable=size_note,
        font=("Segoe UI", 12),
        fg=SETUP_MUTED,
        bg=SETUP_PANEL,
        wraplength=480,
        justify="left",
    ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(16, 0))

    shape_combo = ttk.Combobox(
        form_right,
        textvariable=shape_display,
        values=[lbl for lbl, _ in shape_items],
        state="readonly",
        width=combo_width,
    )
    add_row(form_right, 0, "Camera shape", shape_combo)

    size_combo = ttk.Combobox(
        form_right,
        textvariable=size_display,
        values=[lbl for lbl, _ in size_items],
        state="readonly",
        width=combo_width,
    )
    add_row(form_right, 1, "Camera size", size_combo)

    position_combo = ttk.Combobox(
        form_right,
        textvariable=position_display,
        values=[lbl for lbl, _ in position_items],
        state="readonly",
        width=combo_width,
    )
    add_row(form_right, 2, "Camera position", position_combo)

    zoom_combo = ttk.Combobox(
        form_right,
        textvariable=zoom_display,
        values=[lbl for lbl, _ in zoom_items],
        state="readonly",
        width=combo_width,
    )
    add_row(form_right, 3, "Camera zoom", zoom_combo)

    rotation_combo = ttk.Combobox(
        form_right,
        textvariable=rotation_display,
        values=[lbl for lbl, _ in rotation_items],
        state="readonly",
        width=combo_width,
    )
    add_row(form_right, 4, "Camera rotation", rotation_combo)

    footer = tk.Frame(outer, bg=SETUP_BG, padx=48, pady=24)
    footer.pack(fill="x", side="bottom")

    tk.Button(
        footer,
        text="Settings",
        font=("Segoe UI", 13),
        fg=SETUP_TEXT,
        bg=SETUP_CARD,
        activebackground=SETUP_BORDER,
        relief="flat",
        padx=22,
        pady=12,
        cursor="hand2",
        command=lambda: open_shortcuts_settings(root, shortcut_vars),
    ).pack(side="left")

    tk.Button(
        footer,
        text="Cancel",
        font=("Segoe UI", 13),
        fg=SETUP_TEXT,
        bg=SETUP_CARD,
        activebackground=SETUP_BORDER,
        relief="flat",
        padx=22,
        pady=12,
        cursor="hand2",
        command=close_setup,
    ).pack(side="right", padx=(12, 0))

    tk.Button(
        footer,
        text="Start recording",
        font=("Segoe UI", 14, "bold"),
        fg=SETUP_BG,
        bg=SETUP_ACCENT,
        activebackground=SETUP_ACCENT_HOVER,
        activeforeground=SETUP_BG,
        relief="flat",
        padx=28,
        pady=14,
        cursor="hand2",
        command=start,
    ).pack(side="right")

    quality_combo.bind("<<ComboboxSelected>>", on_quality_change)
    ratio_combo.bind("<<ComboboxSelected>>", lambda _e: on_settings_change())
    fps_combo.bind("<<ComboboxSelected>>", lambda _e: on_settings_change())
    camera_combo.bind("<<ComboboxSelected>>", on_camera_combo_change)
    shape_combo.bind("<<ComboboxSelected>>", on_camera_layout_change)
    size_combo.bind("<<ComboboxSelected>>", on_camera_layout_change)
    position_combo.bind("<<ComboboxSelected>>", on_camera_layout_change)
    zoom_combo.bind("<<ComboboxSelected>>", on_camera_layout_change)
    rotation_combo.bind("<<ComboboxSelected>>", on_camera_layout_change)
    custom_w.trace_add("write", on_settings_change)
    custom_h.trace_add("write", on_settings_change)

    w0, h0 = current_size_for(ratio_key())
    if w0 < 64:
        w0, h0 = screen_fit(*size_for_quality("16:9", quality_key()))
    preview = CyanBorder(root, w0, h0, BORDER_WIDTH, show_label=True)
    preview_holder["ov"] = preview

    drag_state: dict[str, bool] = {"active": False}

    def on_preview_press(event: tk.Event) -> None:
        if cam_state.get("webcam") is None:
            return
        drag_state["active"] = True

    def on_preview_drag(event: tk.Event) -> None:
        if not drag_state.get("active"):
            return
        ov = preview_holder.get("ov")
        if ov is None:
            return
        enc_w, enc_h = current_encode_size()
        fw, fh = webcam_frame_dims()
        rot = camera_rotation_value()
        disp_w, disp_h = (
            overlay_source_dims(fw, fh, rot) if fw and fh else (fw, fh)
        )
        cam_w, cam_h, _, _ = camera_overlay_pixels(
            enc_w,
            enc_h,
            camera_size_key(),
            camera_position_key(),
            frame_w=disp_w,
            frame_h=disp_h,
            zoom=camera_zoom_value(),
            rotation_deg=rot,
        )
        ox = int(round(event.x * enc_w / max(1, ov.box_w)))
        oy = int(round(event.y * enc_h / max(1, ov.box_h)))
        ox = max(0, min(ox, enc_w - cam_w))
        oy = max(0, min(oy, enc_h - cam_h))
        on_camera_moved(ox, oy)
        refresh_camera_overlay()

    def on_preview_release(_event: tk.Event) -> None:
        drag_state["active"] = False

    preview.canvas.bind("<ButtonPress-1>", on_preview_press)
    preview.canvas.bind("<B1-Motion>", on_preview_drag)
    preview.canvas.bind("<ButtonRelease-1>", on_preview_release)
    preview.canvas.configure(cursor="hand2")

    refresh_ratio_dropdown("16:9")
    update_custom_state()
    update_size_note()
    apply_preset_position()
    update_preview()
    lift_preview_above_settings(root, preview)
    root.after(300, sync_setup_webcam)
    root.update_idletasks()

    root.protocol("WM_DELETE_WINDOW", close_setup)
    root.mainloop()
    stop_setup_webcam()
    try:
        preview.destroy()
    except Exception:
        pass
    return result


def ask_setup_cli(mics: list[str], catalog: list[CameraDevice]) -> dict | None:
    print()
    print("  Cursor Follower — Record")
    print("  ------------------------")
    print()
    print("  Video quality")
    keys = list(QUALITY_PRESETS)
    for i, key in enumerate(keys, start=1):
        p = QUALITY_PRESETS[key]
        default = "  (recommended)" if key == "hd" else ""
        print(f"    {i}) {p['label']:3}  —  {p['detail']}{default}")
    print()
    quality = "hd"
    q_max = str(len(keys))
    while True:
        raw = input(f"  Choose quality [1-{q_max}] (default 2): ").strip() or "2"
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            quality = keys[int(raw) - 1]
            break
        print(f"  Please enter a number from 1 to {q_max}.")

    print()
    print("  Frame rate")
    for i, fps_val in enumerate(FPS_CHOICES, start=1):
        extra = "  (recommended)" if fps_val == 30 else ""
        print(f"    {i}) {fps_val} fps{extra}")
    print()
    fps = 30
    fps_max = str(len(FPS_CHOICES))
    while True:
        raw = input(f"  Choose frame rate [1-{fps_max}] (default 2): ").strip() or "2"
        if raw.isdigit() and 1 <= int(raw) <= len(FPS_CHOICES):
            fps = int(FPS_CHOICES[int(raw) - 1])
            break
        print(f"  Please enter a number from 1 to {fps_max}.")

    print()
    print("  Microphone")
    mic_name: str | None = None
    if mics:
        default = preferred_microphone(mics) or mics[0]
        default_idx = mics.index(default) + 1
        print(f"    Auto-detected: {default}")
        print()
        for i, name in enumerate(mics, start=1):
            tag = "  [default]" if name == default else ""
            print(f"    {i}) {name}{tag}")
        print(f"    {len(mics) + 1}) No audio (screen only)")
        print()
        while True:
            raw = input(
                f"  Choose microphone [1-{len(mics) + 1}] (default {default_idx}): "
            ).strip() or str(default_idx)
            try:
                idx = int(raw)
            except ValueError:
                print("  Please enter a number from the list.")
                continue
            if 1 <= idx <= len(mics):
                mic_name = mics[idx - 1]
                break
            if idx == len(mics) + 1:
                mic_name = None
                break
            print("  That number is not in the list.")
    else:
        print("    None found. Recording will be screen-only.")
        input("  Press Enter to continue...")

    print()
    print("  Webcam overlay")
    camera_name: str | None = None
    camera_shape = "circle"
    camera_size = "medium"
    camera_position = "bottom_right"
    if catalog:
        cameras = [d.name for d in catalog]
        default_cam = preferred_camera(cameras) or cameras[0]
        default_cam_idx = cameras.index(default_cam) + 1
        print(f"    Auto-detected: {default_cam}")
        print()
        for i, dev in enumerate(catalog, start=1):
            tag = "  [default]" if dev.name == default_cam else ""
            status = ""
            if not dev.driver_ok:
                status = f"  [{dev.pnp_status} — fix in Device Manager]"
            print(f"    {i}) {dev.name}{tag}{status}")
        print(f"    {len(cameras) + 1}) No webcam overlay")
        print()
        while True:
            raw = input(
                f"  Choose camera [1-{len(cameras) + 1}] (default {default_cam_idx}): "
            ).strip() or str(default_cam_idx)
            try:
                idx = int(raw)
            except ValueError:
                print("  Please enter a number from the list.")
                continue
            if 1 <= idx <= len(cameras):
                camera_name = cameras[idx - 1]
                break
            if idx == len(cameras) + 1:
                camera_name = None
                break
            print("  That number is not in the list.")
        if camera_name:
            print()
            print("  Shape:  1) Circle   2) Square")
            shape_raw = input("  Choose shape [1/2] (default 1): ").strip() or "1"
            camera_shape = "square" if shape_raw == "2" else "circle"
            print()
            print("  Size:  1) Small   2) Medium   3) Large")
            size_raw = input("  Choose size [1/2/3] (default 2): ").strip() or "2"
            camera_size = {"1": "small", "2": "medium", "3": "large"}.get(size_raw, "medium")
            print()
            print("  Position:")
            for i, key in enumerate(CAMERA_POSITIONS, start=1):
                print(f"    {i}) {CAMERA_POSITION_LABELS[key]}")
            pos_raw = input("  Choose position [1-4] (default 4): ").strip() or "4"
            try:
                pos_idx = int(pos_raw)
                if 1 <= pos_idx <= len(CAMERA_POSITIONS):
                    camera_position = CAMERA_POSITIONS[pos_idx - 1]
            except ValueError:
                pass
    else:
        print("    None found. No webcam overlay.")

    print()
    enc16 = size_for_quality("16:9", quality)
    enc916 = size_for_quality("9:16", quality)
    w16, h16 = screen_fit(*enc16)
    w916, h916 = screen_fit(*enc916)
    print("  Frame size (the following border — fitted to this screen)")
    print(f"    1) 16:9  widescreen  {w16}x{h16}   (file {enc16[0]}x{enc16[1]})")
    print(f"    2) 9:16  vertical    {w916}x{h916}   (file {enc916[0]}x{enc916[1]})")
    print("    3) Custom width x height")
    print()
    encode_w: int
    encode_h: int
    while True:
        raw = input("  Choose size [1/2/3]: ").strip()
        if raw == "1":
            ratio, width, height = "16:9", w16, h16
            encode_w, encode_h = enc16
            break
        if raw == "2":
            ratio, width, height = "9:16", w916, h916
            encode_w, encode_h = enc916
            break
        if raw == "3":
            try:
                native_w = even(int(input("  Width  (pixels): ").strip()))
                native_h = even(int(input("  Height (pixels): ").strip()))
            except ValueError:
                print("  Please enter whole numbers.")
                continue
            if native_w < 64 or native_h < 64:
                print("  Size must be at least 64x64.")
                continue
            width, height = screen_fit(native_w, native_h)
            encode_w, encode_h = native_w, native_h
            ratio = f"{native_w}:{native_h}"
            break
        print("  Please enter 1, 2, or 3.")

    return {
        "quality": quality,
        "fps": fps,
        "ratio": ratio,
        "width": width,
        "height": height,
        "encode_width": encode_w,
        "encode_height": encode_h,
        "mic_name": mic_name,
        "camera_name": camera_name,
        "camera_shape": camera_shape,
        "camera_size": camera_size,
        "camera_position": camera_position,
        "camera_ox": None,
        "camera_oy": None,
        "camera_zoom": 1.0,
        "camera_rotation": 0,
        "shortcuts": dict(DEFAULT_SHORTCUTS),
        "record": True,
    }


def resolve_session(
    args: argparse.Namespace,
    mics: list[str],
    catalog: list[CameraDevice],
    working_names: set[str] | None = None,
) -> dict | None:
    """Build a session dict from CLI flags, or open the setup prompt."""
    overlay_only = bool(args.overlay_only)
    fps = int(args.fps) if args.fps else 30
    fully_specified = bool(args.quality) and bool(args.ratio or (args.width and args.height))
    if overlay_only:
        ratio = args.ratio or "16:9"
        if args.width and args.height:
            encode_w, encode_h = even(args.width), even(args.height)
            width, height = screen_fit(encode_w, encode_h)
            ratio = f"{encode_w}:{encode_h}"
        else:
            width, height = screen_fit(*configured_size(ratio))
            encode_w, encode_h = width, height
        return {
            "quality": args.quality or "hd",
            "fps": fps,
            "ratio": ratio,
            "width": width,
            "height": height,
            "encode_width": encode_w,
            "encode_height": encode_h,
            "mic_name": None,
            "camera_name": None,
            "camera_shape": "circle",
            "camera_size": "medium",
            "camera_position": "bottom_right",
            "camera_ox": None,
            "camera_oy": None,
            "camera_zoom": 1.0,
            "camera_rotation": 0,
            "shortcuts": dict(DEFAULT_SHORTCUTS),
            "record": False,
        }

    if fully_specified:
        quality = args.quality or "hd"
        if args.width and args.height:
            encode_w, encode_h = even(args.width), even(args.height)
            width, height = screen_fit(encode_w, encode_h)
            ratio = f"{encode_w}:{encode_h}"
        else:
            ratio = args.ratio or "16:9"
            encode_w, encode_h = size_for_quality(ratio, quality)
            width, height = screen_fit(encode_w, encode_h)
        mic_name: str | None
        if args.mic is None:
            mic_name = preferred_microphone(mics)
        elif args.mic.strip().lower() in ("none", "off", "no"):
            mic_name = None
        else:
            mic_name = args.mic
        return {
            "quality": quality,
            "fps": fps,
            "ratio": ratio,
            "width": width,
            "height": height,
            "encode_width": encode_w,
            "encode_height": encode_h,
            "mic_name": mic_name,
            "camera_name": preferred_camera([d.name for d in catalog]),
            "camera_shape": "circle",
            "camera_size": "medium",
            "camera_position": "bottom_right",
            "camera_ox": None,
            "camera_oy": None,
            "camera_zoom": 1.0,
            "camera_rotation": 0,
            "shortcuts": dict(DEFAULT_SHORTCUTS),
            "record": True,
        }

    if args.cli:
        return ask_setup_cli(mics, catalog)
    return ask_setup_gui(mics, catalog, working_names=working_names)


# -----------------------------------------------------------------------------
# Overlay + record
# -----------------------------------------------------------------------------


def run(
    ratio: str,
    border_w: int,
    width: int,
    height: int,
    *,
    record: bool,
    quality: str,
    fps: int,
    encode_width: int,
    encode_height: int,
    mic_name: str | None,
    camera_name: str | None,
    camera_shape: str,
    camera_size: str,
    camera_position: str,
    camera_ox: int | None,
    camera_oy: int | None,
    camera_zoom: float,
    camera_rotation: int,
    catalog: list[CameraDevice],
    webcam_capture: WebcamCapture | None,
    ffmpeg: str | None,
    shortcuts: dict[str, str] | None = None,
) -> None:
    box_w, box_h = screen_fit(max(50, int(width)), max(50, int(height)))
    border_w = max(2, min(30, int(border_w)))
    fps = int(fps) if fps in FPS_CHOICES else 30
    encode_w = even(max(64, int(encode_width)))
    encode_h = even(max(64, int(encode_height)))
    camera_shape = camera_shape if camera_shape in CAMERA_SHAPES else "circle"
    camera_size = camera_size if camera_size in CAMERA_SIZES else "medium"
    camera_position = (
        camera_position if camera_position in CAMERA_POSITIONS else "bottom_right"
    )
    camera_zoom = max(0.5, min(4.0, float(camera_zoom)))
    camera_rotation = int(camera_rotation) % 360
    hotkey_cfg = dict(DEFAULT_SHORTCUTS)
    if shortcuts:
        hotkey_cfg.update(shortcuts)
    hotkeys = {key: parse_hotkey(val) for key, val in hotkey_cfg.items()}

    webcam: WebcamCapture | None = webcam_capture
    if camera_name and (camera_ox is None or camera_oy is None):
        fw, fh = None, None
        if webcam is not None:
            wf = webcam.get_frame()
            if wf is not None:
                fh, fw = wf.shape[0], wf.shape[1]
        disp_w, disp_h = (
            overlay_source_dims(fw, fh, camera_rotation) if fw and fh else (fw, fh)
        )
        _, _, camera_ox, camera_oy = camera_overlay_pixels(
            encode_w,
            encode_h,
            camera_size,
            camera_position,
            frame_w=disp_w,
            frame_h=disp_h,
            zoom=camera_zoom,
            rotation_deg=camera_rotation,
        )

    overlay = CyanBorder(None, box_w, box_h, border_w, show_label=False)
    root = overlay.win
    overlay.set_click_through(True)
    recorder: ScreenRecorder | None = None
    hud: RecordHud | None = None
    stopping = {"done": False}
    follow = {"locked": False, "center": get_cursor_pos(), "park_was": False}
    take = {"active": False, "frozen": 0.0, "clock": ""}
    zoom = {
        "level": 1.0,
        "base_w": box_w,
        "base_h": box_h,
        "clock": time.perf_counter(),
    }
    cam_live: dict = {
        "ox": int(camera_ox or 0),
        "oy": int(camera_oy or 0),
        "zoom": camera_zoom,
        "rotation": camera_rotation,
        "visible": True,
        "position": camera_position,
    }
    keys_was: dict[int, bool] = {}
    drag_cam = {"active": False}

    if not camera_name and webcam is not None:
        webcam.stop()
        webcam = None

    if camera_name:
        dev = find_camera(camera_name, catalog)
        if webcam is None and dev is not None:
            try:
                webcam = WebcamCapture.open_device(dev, catalog=catalog)
                print(f"  Webcam overlay: {camera_name}")
            except Exception as exc:  # noqa: BLE001
                print(f"  Could not open camera ({exc}). Continuing without webcam overlay.")
                webcam = None
        elif webcam is not None:
            print(f"  Webcam overlay: {camera_name} (from setup)")

    def get_frame() -> tuple[int, int, int, int]:
        """Center + current capture size (grows/shrinks with zoom, aspect locked)."""
        x, y = overlay.top_left
        return int(x + overlay.box_w // 2), int(y + overlay.box_h // 2), overlay.box_w, overlay.box_h

    def place_overlay() -> None:
        cx, cy = follow["center"]
        overlay.follow_center(int(cx), int(cy))

    def set_zoom_level(level: float) -> None:
        level = max(ZOOM_MIN, min(ZOOM_MAX, float(level)))
        w = even(max(64, round(zoom["base_w"] * level)))
        h = even(max(64, round(w * zoom["base_h"] / zoom["base_w"])))
        long_edge = max(w, h)
        if long_edge > 8192:
            scale = 8192 / long_edge
            w = even(max(64, round(w * scale)))
            h = even(max(64, round(h * scale)))
        zoom["level"] = level
        if (w, h) == (overlay.box_w, overlay.box_h):
            return
        overlay.set_size(w, h, fit_to_screen=False)
        place_overlay()
        if webcam is not None:
            overlay.clear_webcam_overlay()

    def move_to_cursor() -> None:
        if follow["locked"]:
            return
        cx, cy = get_cursor_pos()
        follow["center"] = (int(cx), int(cy))
        overlay.follow_center(int(cx), int(cy))

    def toggle_follow() -> None:
        follow["locked"] = not follow["locked"]
        overlay.set_click_through(not follow["locked"])
        if follow["locked"]:
            overlay.redraw(BORDER_COLOR_LOCKED)
            overlay.canvas.configure(cursor="hand2")
            print("  Border parked — drag the camera inside the frame. Hotkey again to follow.")
        else:
            overlay.redraw(BORDER_COLOR)
            overlay.canvas.configure(cursor="")
            print("  Border following the pointer again.")

    def cam_frame_dims() -> tuple[int | None, int | None]:
        if webcam is None:
            return None, None
        frame = webcam.get_frame()
        if frame is None:
            return None, None
        return int(frame.shape[1]), int(frame.shape[0])

    def snap_camera(position_key: str) -> None:
        if webcam is None:
            return
        fw, fh = cam_frame_dims()
        disp_w, disp_h = (
            overlay_source_dims(fw, fh, cam_live["rotation"]) if fw and fh else (None, None)
        )
        _, _, ox, oy = camera_overlay_pixels(
            encode_w,
            encode_h,
            camera_size,
            position_key,
            frame_w=disp_w,
            frame_h=disp_h,
            zoom=float(cam_live["zoom"]),
            rotation_deg=int(cam_live["rotation"]),
        )
        cam_live["ox"] = ox
        cam_live["oy"] = oy
        cam_live["position"] = position_key

    def on_overlay_press(event: tk.Event) -> None:
        if not follow["locked"] or not cam_live["visible"] or webcam is None:
            return
        fw, fh = cam_frame_dims()
        disp_w, disp_h = (
            overlay_source_dims(fw, fh, cam_live["rotation"]) if fw and fh else (None, None)
        )
        sx, sy, sw, sh = overlay.webcam_screen_rect(
            encode_w,
            encode_h,
            int(cam_live["ox"]),
            int(cam_live["oy"]),
            camera_size,
            disp_w,
            disp_h,
            float(cam_live["zoom"]),
            int(cam_live["rotation"]),
        )
        if sx <= event.x <= sx + sw and sy <= event.y <= sy + sh:
            drag_cam["active"] = True

    def on_overlay_drag(event: tk.Event) -> None:
        if not drag_cam["active"]:
            return
        fw, fh = cam_frame_dims()
        disp_w, disp_h = (
            overlay_source_dims(fw, fh, cam_live["rotation"]) if fw and fh else (None, None)
        )
        cam_w, cam_h, _, _ = camera_overlay_pixels(
            encode_w,
            encode_h,
            camera_size,
            "bottom_right",
            frame_w=disp_w,
            frame_h=disp_h,
            zoom=float(cam_live["zoom"]),
            rotation_deg=int(cam_live["rotation"]),
        )
        ox = int(round(event.x * encode_w / max(1, overlay.box_w)))
        oy = int(round(event.y * encode_h / max(1, overlay.box_h)))
        cam_live["ox"] = max(0, min(ox, encode_w - cam_w))
        cam_live["oy"] = max(0, min(oy, encode_h - cam_h))
        cam_live["position"] = "custom"

    overlay.canvas.bind("<ButtonPress-1>", on_overlay_press)
    overlay.canvas.bind("<B1-Motion>", on_overlay_drag)
    overlay.canvas.bind("<ButtonRelease-1>", lambda _e: drag_cam.update(active=False))

    def elapsed_now() -> float:
        if take["active"] and recorder is not None:
            return recorder.elapsed_s()
        return float(take["frozen"])

    def sync_hud() -> None:
        if hud is None:
            return
        elapsed = elapsed_now()
        clock = _format_elapsed(elapsed)
        label_key = f"{take['active']}:{clock}"
        if take["clock"] != label_key:
            take["clock"] = label_key
            hud.set_recording(bool(take["active"]), elapsed)
        hud.place_bottom_left(*follow["center"])

    def make_recorder() -> ScreenRecorder:
        out = default_output_path(ratio, quality, encode_w, encode_h, fps)
        q = QUALITY_PRESETS[quality]
        return ScreenRecorder(
            width=encode_w,
            height=encode_h,
            fps=fps,
            crf=int(q["crf"]),
            x264_preset=encoder_preset(quality, fps),
            mic_name=mic_name,
            audio_bitrate=str(q["audio_bitrate"]),
            output_path=out,
            get_frame=get_frame,
            webcam=webcam,
            camera_shape=camera_shape,
            camera_size=camera_size,
            camera_position=camera_position,
            camera_state=cam_live,
        )

    def begin_take() -> bool:
        nonlocal recorder
        if take["active"] and recorder is not None:
            return True
        if ffmpeg is None:
            return False
        rec = make_recorder()
        try:
            rec.start(ffmpeg)
        except Exception as exc:  # noqa: BLE001
            print(f"  Could not start recorder: {exc}")
            return False
        recorder = rec
        take["active"] = True
        take["frozen"] = 0.0
        take["clock"] = ""
        print(f"  Recording: {rec.output_path}")
        sync_hud()
        return True

    def hud_stop() -> None:
        nonlocal recorder
        if recorder is None or not take["active"]:
            return
        print("  Stopping recorder...")
        take["frozen"] = recorder.elapsed_s()
        frames = recorder.frames_written
        saved = None
        try:
            saved = recorder.stop()
        except RuntimeError as exc:
            print(f"  Recorder error: {exc}")
        recorder = None
        take["active"] = False
        take["clock"] = ""
        if saved:
            mins, secs = divmod(int(take["frozen"]), 60)
            print(f"  Saved: {saved}")
            print(f"  Length: {mins:02d}:{secs:02d}  ({frames} frames)")
        sync_hud()

    def hud_start() -> None:
        begin_take()

    def hud_refresh() -> None:
        if take["active"]:
            hud_stop()
        take["frozen"] = 0.0
        begin_take()

    def finish() -> None:
        if stopping["done"]:
            return
        stopping["done"] = True
        saved = None
        elapsed = 0.0
        frames = 0
        if recorder is not None and take["active"]:
            print("  Stopping recorder...")
            elapsed = recorder.elapsed_s()
            frames = recorder.frames_written
            try:
                saved = recorder.stop()
            except RuntimeError as exc:
                print(f"  Recorder error: {exc}")
            take["active"] = False
        if hud is not None:
            hud.destroy()
        if webcam is not None:
            webcam.stop()
        overlay.destroy()
        if saved:
            mins, secs = divmod(int(elapsed), 60)
            print()
            print(f"  Saved: {saved}")
            print(f"  Length: {mins:02d}:{secs:02d}  ({frames} frames)")

    def tick() -> None:
        if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
            finish()
            return
        ctrl_down = bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
        shift_down = bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
        alt_down = bool(user32.GetAsyncKeyState(VK_MENU) & 0x8000)
        caps_down = bool(user32.GetAsyncKeyState(VK_CAPITAL) & 0x8000)

        park_b = hotkeys["park_follow"]
        park_active = modifiers_match(park_b, ctrl_down, shift_down, alt_down, caps_down)
        if park_b.get("vk") is None:
            if park_active and not follow["park_was"]:
                toggle_follow()
            follow["park_was"] = park_active
        elif park_active and park_b.get("vk") is not None:
            vk = int(park_b["vk"])
            if key_edge(vk, keys_was):
                toggle_follow()

        now = time.perf_counter()
        dt = max(0.0, min(0.05, now - zoom["clock"]))
        zoom["clock"] = now
        zoom_in_b = hotkeys["zoom_in"]
        zoom_out_b = hotkeys["zoom_out"]
        if zoom_in_b.get("vk") is not None:
            vk_in = int(zoom_in_b["vk"])
            if modifiers_match(zoom_in_b, ctrl_down, shift_down, alt_down, caps_down) and key_down(
                vk_in
            ):
                set_zoom_level(zoom["level"] * (ZOOM_RATE ** dt))
        if zoom_out_b.get("vk") is not None:
            vk_out = int(zoom_out_b["vk"])
            if modifiers_match(zoom_out_b, ctrl_down, shift_down, alt_down, caps_down) and key_down(
                vk_out
            ):
                set_zoom_level(zoom["level"] * ((1.0 / ZOOM_RATE) ** dt))

        for action, position_key in (
            ("cam_top_left", "top_left"),
            ("cam_top_right", "top_right"),
            ("cam_bottom_left", "bottom_left"),
            ("cam_bottom_right", "bottom_right"),
        ):
            binding = hotkeys[action]
            vk = binding.get("vk")
            if vk is None:
                continue
            if modifiers_match(binding, ctrl_down, shift_down, alt_down, caps_down):
                if key_edge(int(vk), keys_was):
                    snap_camera(position_key)

        toggle_b = hotkeys["cam_toggle"]
        toggle_vk = toggle_b.get("vk")
        if toggle_vk is not None and modifiers_match(
            toggle_b, ctrl_down, shift_down, alt_down, caps_down
        ):
            if key_edge(int(toggle_vk), keys_was):
                cam_live["visible"] = not cam_live["visible"]
                if not cam_live["visible"]:
                    overlay.clear_webcam_overlay()
                state = "shown" if cam_live["visible"] else "hidden"
                print(f"  Camera overlay {state}.")

        move_to_cursor()
        sync_hud()
        if webcam is not None and cam_live["visible"]:
            frame = webcam.get_frame()
            if frame is not None:
                try:
                    overlay.update_webcam_overlay(
                        frame,
                        encode_w,
                        encode_h,
                        int(cam_live["ox"]),
                        int(cam_live["oy"]),
                        camera_shape,
                        camera_size,
                        float(cam_live["zoom"]),
                        int(cam_live["rotation"]),
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"  Webcam preview error: {exc}")
        elif webcam is not None and not cam_live["visible"]:
            overlay.clear_webcam_overlay()
        try:
            root.after(UPDATE_MS, tick)
        except tk.TclError:
            return

    user32.SetWindowPos(
        overlay.hwnd,
        HWND_TOPMOST,
        0,
        0,
        0,
        0,
        SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_NOMOVE,
    )

    q = QUALITY_PRESETS[quality]
    print()
    if record:
        print(
            f"  Recording  {ratio}   on-screen {box_w}x{box_h}   "
            f"file {encode_w}x{encode_h}   {q['label']} @ {fps} fps"
        )
        if mic_name:
            print(f"  Microphone: {mic_name}")
        else:
            print("  Microphone: off")
        print("  Cyan border = captured area (cursor stays in the center).")
        print("  Red rec dot is in the bottom-left corner. Timer always; hover for Start / Stop / Refresh / Exit.")
        print("  Start waits 3-2-1. Stop is immediate. Exit saves and quits.")
        print(f"  Park / follow: {hotkey_cfg['park_follow']}")
        print(f"  Zoom border: {hotkey_cfg['zoom_out']} / {hotkey_cfg['zoom_in']}")
        if webcam is not None:
            print(
                f"  Camera corners: {hotkey_cfg['cam_top_left']}, "
                f"{hotkey_cfg['cam_top_right']}, {hotkey_cfg['cam_bottom_left']}, "
                f"{hotkey_cfg['cam_bottom_right']}"
            )
            print(f"  Show / hide camera: {hotkey_cfg['cam_toggle']}")
            print("  While parked, drag the camera inside the cyan frame.")
        print("  Press Esc to quit.")
        hud = RecordHud(root, hud_start, hud_stop, hud_refresh, finish)
        hud.countdown(lambda: begin_take())
    else:
        print(f"  Overlay only: {ratio}   {box_w}x{box_h}")
        print(f"  Park / follow: {hotkey_cfg['park_follow']}")
        print(f"  Zoom: {hotkey_cfg['zoom_out']} / {hotkey_cfg['zoom_in']}")
        print("  Press Esc to quit.")

    root.protocol("WM_DELETE_WINDOW", finish)
    move_to_cursor()
    tick()
    try:
        root.mainloop()
    finally:
        finish()


def main() -> int:
    enable_dpi_awareness()
    args = parse_args()
    print()
    print("  Cursor Follower")
    print("  ---------------")
    try:
        ensure_recording_deps()
    except Exception as exc:  # noqa: BLE001
        print(f"  Could not install packages: {exc}")
        print("  Try:  py -3 -m pip install -r requirements.txt")
        return 1

    ffmpeg = None
    mics: list[str] = []
    catalog: list[CameraDevice] = []
    working_names: set[str] = set()
    if not args.overlay_only:
        try:
            ffmpeg = find_ffmpeg()
        except FileNotFoundError as exc:
            print(f"  {exc}")
            return 1
        try:
            mics = list_microphones(ffmpeg)
        except Exception as exc:  # noqa: BLE001
            print(f"  Could not list microphones ({exc}). Continuing without a default mic.")
            mics = []
        try:
            catalog = discover_cameras(ffmpeg)
        except Exception as exc:  # noqa: BLE001
            print(f"  Could not list cameras ({exc}). Continuing without webcam overlay.")
            catalog = []
        cameras = [d.name for d in catalog]
        working_list = probe_cameras(ffmpeg) if catalog else []
        working_names = set(working_list)
        if catalog:
            print(f"  Cameras found: {', '.join(cameras)}")
            if working_list:
                print(f"  Ready now: {', '.join(working_list)}")
            offline = [c for c in cameras if c not in working_names]
            if offline:
                print(f"  Not ready yet: {', '.join(offline)}")
            for dev in catalog:
                if not dev.driver_ok and dev.backend != "http":
                    print(
                        f"  Warning: {dev.name} driver is {dev.pnp_status} — "
                        "in Device Manager disable then enable it, or reinstall DroidCam."
                    )
        else:
            print("  No cameras detected.")

        if mics:
            print(f"  Auto-detected microphone: {preferred_microphone(mics)}")
        else:
            print("  No microphones detected.")

    session = resolve_session(args, mics, catalog, working_names)
    if session is None:
        print("  Cancelled.")
        return 0

    try:
        run(
            session["ratio"],
            args.border,
            session["width"],
            session["height"],
            record=session["record"],
            quality=session["quality"],
            fps=int(session.get("fps") or 30),
            encode_width=int(session.get("encode_width") or session["width"]),
            encode_height=int(session.get("encode_height") or session["height"]),
            mic_name=session["mic_name"],
            camera_name=session.get("camera_name"),
            camera_shape=str(session.get("camera_shape") or "square"),
            camera_size=str(session.get("camera_size") or "medium"),
            camera_position=str(session.get("camera_position") or "bottom_right"),
            camera_ox=session.get("camera_ox"),
            camera_oy=session.get("camera_oy"),
            camera_zoom=float(session.get("camera_zoom") or 1.0),
            camera_rotation=int(session.get("camera_rotation") or 0),
            shortcuts=session.get("shortcuts"),
            catalog=catalog,
            webcam_capture=session.get("webcam_capture"),
            ffmpeg=ffmpeg,
        )
    except KeyboardInterrupt:
        print("\n  Exiting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
