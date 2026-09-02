"""
Cursor-centered aspect-ratio border overlay for Windows.

Shows a setup prompt (quality, microphone, frame size) with a live cyan
preview of the chosen size, then records the screen inside that rectangle
with the chosen microphone. Press Ctrl+Caps Lock to park the border.
Press Ctrl+Caps Lock again to follow the pointer.
Hold Ctrl+Shift and Right to zoom in, or Left to zoom out (smooth, same ratio).
Press Esc to stop and save.
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
    FPS_CHOICES,
    QUALITY_PRESETS,
    ScreenRecorder,
    default_output_path,
    encoder_preset,
    ensure_recording_deps,
    even,
    find_ffmpeg,
    list_microphones,
    preferred_microphone,
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
        return self.last_pos if self.last_pos is not None else (0, 0)

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


def ask_setup_gui(mics: list[str]) -> dict | None:
    """Quality / microphone / frame-size window. None if the user cancels."""
    result: dict | None = None
    root = tk.Tk()
    root.title("Cursor Follower — Record")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    quality_var = tk.StringVar(value="hd")
    fps_var = tk.IntVar(value=30)
    ratio_var = tk.StringVar(value="16:9")
    custom_w = tk.StringVar(value="1920")
    custom_h = tk.StringVar(value="1080")
    mic_choices = _mic_choices(mics)
    mic_display = tk.StringVar(value=mic_choices[0][0])
    size_note = tk.StringVar()

    def current_size_for(ratio: str) -> tuple[int, int]:
        q = quality_var.get()
        if ratio == "custom":
            try:
                raw_w, raw_h = even(int(custom_w.get())), even(int(custom_h.get()))
            except ValueError:
                return 0, 0
            if raw_w < 64 or raw_h < 64:
                return raw_w, raw_h
            return screen_fit(raw_w, raw_h)
        return screen_fit(*size_for_quality(ratio, q))

    def refresh_size_labels() -> None:
        raw16 = size_for_quality("16:9", quality_var.get())
        raw916 = size_for_quality("9:16", quality_var.get())
        w16, h16 = screen_fit(*raw16)
        w916, h916 = screen_fit(*raw916)
        fit16 = "" if (w16, h16) == raw16 else "  (fits screen)"
        fit916 = "" if (w916, h916) == raw916 else "  (fits screen)"
        radio_169.config(text=f"16:9 widescreen   {w16} × {h16}{fit16}")
        radio_916.config(text=f"9:16 vertical     {w916} × {h916}{fit916}")
        if ratio_var.get() != "custom":
            w, h = current_size_for(ratio_var.get())
            custom_w.set(str(w))
            custom_h.set(str(h))
        qkey = quality_var.get()
        q = QUALITY_PRESETS[qkey]
        chosen = ratio_var.get()
        w_now, h_now = current_size_for(chosen)
        if chosen == "custom":
            try:
                enc_w, enc_h = even(int(custom_w.get())), even(int(custom_h.get()))
            except ValueError:
                enc_w, enc_h = w_now, h_now
        else:
            enc_w, enc_h = size_for_quality(chosen, qkey)
        if w_now >= 64 and h_now >= 64:
            extra = (
                f"  Saved as {enc_w} × {enc_h}."
                if (enc_w, enc_h) != (w_now, h_now)
                else ""
            )
            size_note.set(
                f"On screen: {w_now} × {h_now} px.{extra}  "
                f"{fps_var.get()} fps ({q['label']})"
            )
        else:
            size_note.set("Enter width and height (at least 64 × 64) to preview the window.")
        custom_state = "normal" if ratio_var.get() == "custom" else "disabled"
        entry_w.configure(state=custom_state)
        entry_h.configure(state=custom_state)
        update_preview()

    pad = {"padx": 16, "pady": 4}
    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="Start recording", font=("Segoe UI", 14, "bold")).grid(
        row=0, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 8)
    )

    ttk.Label(frm, text="1. Video quality", font=("Segoe UI", 10, "bold")).grid(
        row=1, column=0, columnspan=3, sticky="w", **pad
    )
    q_row = 2
    for key, preset in QUALITY_PRESETS.items():
        ttk.Radiobutton(
            frm,
            text=f"{preset['label']}   —  {preset['detail']}",
            variable=quality_var,
            value=key,
            command=refresh_size_labels,
        ).grid(row=q_row, column=0, columnspan=3, sticky="w", padx=28, pady=1)
        q_row += 1

    ttk.Label(frm, text="2. Frame rate", font=("Segoe UI", 10, "bold")).grid(
        row=q_row, column=0, columnspan=3, sticky="w", pady=(12, 4), padx=16
    )
    q_row += 1
    fps_row = ttk.Frame(frm)
    fps_row.grid(row=q_row, column=0, columnspan=3, sticky="w", padx=28, pady=1)
    for fps_val in FPS_CHOICES:
        extra = "  (recommended)" if fps_val == 30 else ""
        ttk.Radiobutton(
            fps_row,
            text=f"{fps_val} fps{extra}",
            variable=fps_var,
            value=fps_val,
            command=refresh_size_labels,
        ).pack(side="left", padx=(0, 16))
    q_row += 1

    ttk.Label(frm, text="3. Microphone", font=("Segoe UI", 10, "bold")).grid(
        row=q_row, column=0, columnspan=3, sticky="w", pady=(12, 4), padx=16
    )
    q_row += 1
    mic_combo = ttk.Combobox(
        frm,
        textvariable=mic_display,
        values=[c[0] for c in mic_choices],
        state="readonly",
        width=52,
    )
    mic_combo.grid(row=q_row, column=0, columnspan=3, sticky="ew", padx=28, pady=2)
    q_row += 1
    if not mics:
        ttk.Label(
            frm,
            text="No microphones found — recording will be screen-only unless you plug one in.",
            foreground="#666666",
        ).grid(row=q_row, column=0, columnspan=3, sticky="w", padx=28)
        q_row += 1

    ttk.Label(frm, text="4. Frame size (the following border)", font=("Segoe UI", 10, "bold")).grid(
        row=q_row, column=0, columnspan=3, sticky="w", pady=(12, 4), padx=16
    )
    q_row += 1
    radio_169 = ttk.Radiobutton(
        frm, variable=ratio_var, value="16:9", command=refresh_size_labels
    )
    radio_169.grid(row=q_row, column=0, columnspan=3, sticky="w", padx=28, pady=1)
    q_row += 1
    radio_916 = ttk.Radiobutton(
        frm, variable=ratio_var, value="9:16", command=refresh_size_labels
    )
    radio_916.grid(row=q_row, column=0, columnspan=3, sticky="w", padx=28, pady=1)
    q_row += 1

    custom_row = ttk.Frame(frm)
    custom_row.grid(row=q_row, column=0, columnspan=3, sticky="w", padx=28, pady=1)
    ttk.Radiobutton(
        custom_row,
        text="Custom",
        variable=ratio_var,
        value="custom",
        command=refresh_size_labels,
    ).pack(side="left")
    entry_w = ttk.Entry(custom_row, textvariable=custom_w, width=7)
    entry_w.pack(side="left", padx=(10, 4))
    ttk.Label(custom_row, text="×").pack(side="left")
    entry_h = ttk.Entry(custom_row, textvariable=custom_h, width=7)
    entry_h.pack(side="left", padx=4)
    ttk.Label(custom_row, text="pixels").pack(side="left")
    q_row += 1

    ttk.Label(frm, textvariable=size_note, foreground="#444444").grid(
        row=q_row, column=0, columnspan=3, sticky="w", padx=16, pady=(10, 2)
    )
    q_row += 1
    ttk.Label(
        frm,
        text="Red rec dot sits in the bottom-left corner. Hover for Start / Stop / Refresh / Exit. 3-2-1 before Start only; Stop is instant.",
        foreground="#444444",
    ).grid(row=q_row, column=0, columnspan=3, sticky="w", padx=16, pady=(0, 4))
    q_row += 1

    def start() -> None:
        nonlocal result
        ratio = ratio_var.get()
        quality = quality_var.get()
        if ratio == "custom":
            try:
                native_w = even(int(custom_w.get().strip()))
                native_h = even(int(custom_h.get().strip()))
            except ValueError:
                messagebox.showerror("Invalid size", "Custom width and height must be whole numbers.")
                return
            if native_w < 64 or native_h < 64:
                messagebox.showerror("Invalid size", "Custom size must be at least 64 × 64.")
                return
            if native_w > 7680 or native_h > 7680:
                messagebox.showerror("Invalid size", "Custom size must be 7680 × 7680 or smaller.")
                return
            width, height = screen_fit(native_w, native_h)
            encode_w, encode_h = native_w, native_h
            ratio_label = f"{native_w}:{native_h}"
        else:
            encode_w, encode_h = size_for_quality(ratio, quality)
            width, height = screen_fit(encode_w, encode_h)
            ratio_label = ratio

        display = mic_display.get()
        mic_name = next((val for label, val in mic_choices if label == display), NO_AUDIO)
        if mic_name == NO_AUDIO:
            mic_name = None

        result = {
            "quality": quality,
            "fps": int(fps_var.get()),
            "ratio": ratio_label,
            "width": width,
            "height": height,
            "encode_width": encode_w,
            "encode_height": encode_h,
            "mic_name": mic_name,
            "record": True,
        }
        try:
            preview.destroy()
        except Exception:
            pass
        root.destroy()

    btns = ttk.Frame(frm)
    btns.grid(row=q_row, column=0, columnspan=3, sticky="e", pady=(12, 4), padx=8)
    ttk.Button(btns, text="Cancel", command=root.destroy).pack(side="right", padx=4)
    ttk.Button(btns, text="Start recording", command=start).pack(side="right", padx=4)

    preview_holder: dict[str, CyanBorder | None] = {"ov": None}

    def update_preview() -> None:
        w, h = current_size_for(ratio_var.get())
        if w < 64 or h < 64:
            return
        ov = preview_holder["ov"]
        if ov is None:
            return
        if ov.box_w != w or ov.box_h != h:
            ov.set_size(w, h)
        ov.center_on_screen()
        ov.lift_behind(root)

    w0, h0 = size_for_quality("16:9", quality_var.get())
    preview = CyanBorder(root, w0, h0, BORDER_WIDTH, show_label=True)
    preview_holder["ov"] = preview

    def on_custom_edit(*_args: object) -> None:
        if ratio_var.get() == "custom":
            update_preview()

    custom_w.trace_add("write", on_custom_edit)
    custom_h.trace_add("write", on_custom_edit)

    refresh_size_labels()
    preview.center_on_screen()
    root.update_idletasks()
    root.geometry("+32+32")
    root.lift()
    root.attributes("-topmost", True)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    try:
        preview.destroy()
    except Exception:
        pass
    return result


def ask_setup_cli(mics: list[str]) -> dict | None:
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
        "record": True,
    }


def resolve_session(args: argparse.Namespace, mics: list[str]) -> dict | None:
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
            "record": True,
        }

    if args.cli:
        return ask_setup_cli(mics)
    return ask_setup_gui(mics)


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
    ffmpeg: str | None,
) -> None:
    box_w, box_h = screen_fit(max(50, int(width)), max(50, int(height)))
    border_w = max(2, min(30, int(border_w)))
    fps = int(fps) if fps in FPS_CHOICES else 30
    encode_w = even(max(64, int(encode_width)))
    encode_h = even(max(64, int(encode_height)))

    overlay = CyanBorder(None, box_w, box_h, border_w, show_label=False)
    root = overlay.win
    recorder: ScreenRecorder | None = None
    hud: RecordHud | None = None
    stopping = {"done": False}
    follow = {"locked": False, "center": get_cursor_pos(), "park_down": True}
    take = {"active": False, "frozen": 0.0, "clock": ""}
    zoom = {
        "level": 1.0,
        "base_w": box_w,
        "base_h": box_h,
        "clock": time.perf_counter(),
    }

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

    def move_to_cursor() -> None:
        if follow["locked"]:
            return
        cx, cy = get_cursor_pos()
        follow["center"] = (int(cx), int(cy))
        overlay.follow_center(int(cx), int(cy))

    def toggle_follow() -> None:
        follow["locked"] = not follow["locked"]
        if follow["locked"]:
            overlay.redraw(BORDER_COLOR_LOCKED)
            print("  Border parked — pointer is free. Ctrl+Caps Lock again to follow.")
        else:
            overlay.redraw(BORDER_COLOR)
            print("  Border following the pointer again.")

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
        caps_down = bool(user32.GetAsyncKeyState(VK_CAPITAL) & 0x8000)
        right_down = bool(user32.GetAsyncKeyState(VK_RIGHT) & 0x8000)
        left_down = bool(user32.GetAsyncKeyState(VK_LEFT) & 0x8000)
        combo = ctrl_down and caps_down
        if combo and not follow["park_down"]:
            toggle_follow()
        follow["park_down"] = combo

        now = time.perf_counter()
        dt = max(0.0, min(0.05, now - zoom["clock"]))
        zoom["clock"] = now
        zoom_mods = ctrl_down and shift_down and not caps_down
        if zoom_mods and right_down and not left_down:
            set_zoom_level(zoom["level"] * (ZOOM_RATE ** dt))
        elif zoom_mods and left_down and not right_down:
            set_zoom_level(zoom["level"] * ((1.0 / ZOOM_RATE) ** dt))

        move_to_cursor()
        sync_hud()
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
        print("  Press Ctrl+Caps Lock to park. Press it again to follow.")
        print("  Hold Ctrl+Shift and Right arrow to zoom in, Left arrow to zoom out (smooth, ratio locked).")
        print("  Press Esc to quit.")
        hud = RecordHud(root, hud_start, hud_stop, hud_refresh, finish)
        hud.countdown(lambda: begin_take())
    else:
        print(f"  Overlay only: {ratio}   {box_w}x{box_h}")
        print("  Press Ctrl+Caps Lock to park. Ctrl+Shift and Left / Right arrow to zoom.")
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
        if mics:
            print(f"  Auto-detected microphone: {preferred_microphone(mics)}")
        else:
            print("  No microphones detected.")

    session = resolve_session(args, mics)
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
            ffmpeg=ffmpeg,
        )
    except KeyboardInterrupt:
        print("\n  Exiting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
