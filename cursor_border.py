"""
Cursor-centered aspect-ratio border overlay for Windows.

Shows a setup prompt (quality, microphone, frame size) with a live cyan
preview of the chosen size, then records the screen inside that rectangle
with the chosen microphone. Press Ctrl+Shift together to park the border in place.
Press Ctrl+Shift again to follow the pointer.
Hold Ctrl+Caps Lock and press + to zoom in, or - to zoom out
(aspect ratio stays 16:9 or 9:16). Press Esc to stop and save.
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
    QUALITY_PRESETS,
    ScreenRecorder,
    default_output_path,
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
VK_OEM_PLUS = 0xBB
VK_OEM_MINUS = 0xBD
VK_ADD = 0x6B
VK_SUBTRACT = 0x6D
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
BORDER_COLOR_LOCKED = "#CCFFFF"  # pale cyan when parked (Ctrl+Shift)

# =============================================================================

KEY_COLOR = "#010101"
KEY_COLORREF = 0x00010101  # 0x00bbggrr for RGB(1,1,1)
UPDATE_MS = 8  # ~120 FPS — keeps cursor locked to box center
ZOOM_STEP = 1.08
ZOOM_MIN = 0.25
ZOOM_MAX = 8.0
ZOOM_REPEAT_S = 0.07


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


def monitor_rect_at(x: int, y: int) -> tuple[int, int, int, int]:
    """Pixel bounds of the display that contains (x, y)."""
    pt = POINT(int(x), int(y))
    handle = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    if not handle:
        return virtual_screen_rect()
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
        return virtual_screen_rect()
    r = info.rcMonitor
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

    def follow_center(self, cx: int, cy: int, *, stay_on_screen: bool | None = None) -> tuple[int, int]:
        """Place the box around (cx, cy). Clamps only while the box still fits on the monitor."""
        x = cx - (self.box_w // 2)
        y = cy - (self.box_h // 2)
        rect = monitor_rect_at(cx, cy)
        mon_w = rect[2] - rect[0]
        mon_h = rect[3] - rect[1]
        if stay_on_screen is None:
            stay_on_screen = self.box_w <= mon_w and self.box_h <= mon_h
        if stay_on_screen:
            x, y = clamp_top_left(x, y, self.box_w, self.box_h, rect)
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
        help="Video quality preset: low, hd, 2k",
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
        q = QUALITY_PRESETS[quality_var.get()]
        w_now, h_now = current_size_for(ratio_var.get())
        if w_now >= 64 and h_now >= 64:
            size_note.set(
                f"Now showing  {w_now} × {h_now} px  — stays fully on this screen.  "
                f"{q['fps']} fps ({q['label']})"
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

    ttk.Label(frm, text="2. Microphone", font=("Segoe UI", 10, "bold")).grid(
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

    ttk.Label(frm, text="3. Frame size (the following border)", font=("Segoe UI", 10, "bold")).grid(
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
        text="Ctrl+Shift parks. Ctrl+Caps Lock and + / - zooms (keeps 16:9 or 9:16). Esc saves.",
        foreground="#444444",
    ).grid(row=q_row, column=0, columnspan=3, sticky="w", padx=16, pady=(0, 4))
    q_row += 1

    def start() -> None:
        nonlocal result
        ratio = ratio_var.get()
        quality = quality_var.get()
        if ratio == "custom":
            try:
                width = even(int(custom_w.get().strip()))
                height = even(int(custom_h.get().strip()))
            except ValueError:
                messagebox.showerror("Invalid size", "Custom width and height must be whole numbers.")
                return
            if width < 64 or height < 64:
                messagebox.showerror("Invalid size", "Custom size must be at least 64 × 64.")
                return
            if width > 7680 or height > 7680:
                messagebox.showerror("Invalid size", "Custom size must be 7680 × 7680 or smaller.")
                return
            width, height = screen_fit(width, height)
            ratio_label = f"{width}:{height}"
        else:
            width, height = screen_fit(*size_for_quality(ratio, quality))
            ratio_label = ratio

        display = mic_display.get()
        mic_name = next((val for label, val in mic_choices if label == display), NO_AUDIO)
        if mic_name == NO_AUDIO:
            mic_name = None

        result = {
            "quality": quality,
            "ratio": ratio_label,
            "width": width,
            "height": height,
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
    while True:
        raw = input("  Choose quality [1/2/3] (default 2): ").strip() or "2"
        if raw in ("1", "2", "3"):
            quality = keys[int(raw) - 1]
            break
        print("  Please enter 1, 2, or 3.")

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
    w16, h16 = screen_fit(*size_for_quality("16:9", quality))
    w916, h916 = screen_fit(*size_for_quality("9:16", quality))
    print("  Frame size (the following border — fitted to this screen)")
    print(f"    1) 16:9  widescreen  {w16}x{h16}")
    print(f"    2) 9:16  vertical    {w916}x{h916}")
    print("    3) Custom width x height")
    print()
    while True:
        raw = input("  Choose size [1/2/3]: ").strip()
        if raw == "1":
            ratio, width, height = "16:9", w16, h16
            break
        if raw == "2":
            ratio, width, height = "9:16", w916, h916
            break
        if raw == "3":
            try:
                width = even(int(input("  Width  (pixels): ").strip()))
                height = even(int(input("  Height (pixels): ").strip()))
            except ValueError:
                print("  Please enter whole numbers.")
                continue
            if width < 64 or height < 64:
                print("  Size must be at least 64x64.")
                continue
            width, height = screen_fit(width, height)
            ratio = f"{width}:{height}"
            break
        print("  Please enter 1, 2, or 3.")

    return {
        "quality": quality,
        "ratio": ratio,
        "width": width,
        "height": height,
        "mic_name": mic_name,
        "record": True,
    }


def resolve_session(args: argparse.Namespace, mics: list[str]) -> dict | None:
    """Build a session dict from CLI flags, or open the setup prompt."""
    overlay_only = bool(args.overlay_only)
    fully_specified = bool(args.quality) and bool(args.ratio or (args.width and args.height))
    if overlay_only:
        ratio = args.ratio or "16:9"
        if args.width and args.height:
            width, height = screen_fit(even(args.width), even(args.height))
            ratio = f"{width}:{height}"
        else:
            width, height = screen_fit(*configured_size(ratio))
        return {
            "quality": args.quality or "hd",
            "ratio": ratio,
            "width": width,
            "height": height,
            "mic_name": None,
            "record": False,
        }

    if fully_specified:
        quality = args.quality or "hd"
        if args.width and args.height:
            width, height = screen_fit(even(args.width), even(args.height))
            ratio = f"{width}:{height}"
        else:
            ratio = args.ratio or "16:9"
            width, height = screen_fit(*size_for_quality(ratio, quality))
        mic_name: str | None
        if args.mic is None:
            mic_name = preferred_microphone(mics)
        elif args.mic.strip().lower() in ("none", "off", "no"):
            mic_name = None
        else:
            mic_name = args.mic
        return {
            "quality": quality,
            "ratio": ratio,
            "width": width,
            "height": height,
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
    mic_name: str | None,
    ffmpeg: str | None,
) -> None:
    box_w, box_h = screen_fit(max(50, int(width)), max(50, int(height)))
    border_w = max(2, min(30, int(border_w)))

    overlay = CyanBorder(None, box_w, box_h, border_w, show_label=False)
    root = overlay.win
    recorder: ScreenRecorder | None = None
    stopping = {"done": False}
    follow = {"locked": False, "center": get_cursor_pos(), "combo_down": True}
    zoom = {
        "level": 1.0,
        "base_w": box_w,
        "base_h": box_h,
        "plus": False,
        "minus": False,
        "last": 0.0,
    }

    def get_frame() -> tuple[int, int, int, int]:
        """Center + current capture size (grows/shrinks with zoom, aspect locked)."""
        cx, cy = follow["center"]
        return int(cx), int(cy), overlay.box_w, overlay.box_h

    def place_overlay() -> None:
        cx, cy = follow["center"]
        follow["center"] = overlay.follow_center(int(cx), int(cy))

    def apply_zoom(factor: float) -> None:
        level = max(ZOOM_MIN, min(ZOOM_MAX, zoom["level"] * factor))
        w = even(max(64, round(zoom["base_w"] * level)))
        h = even(max(64, round(w * zoom["base_h"] / zoom["base_w"])))
        long_edge = max(w, h)
        if long_edge > 8192:
            scale = 8192 / long_edge
            w = even(max(64, round(w * scale)))
            h = even(max(64, round(h * scale)))
        if (w, h) == (overlay.box_w, overlay.box_h) and level == zoom["level"]:
            return
        zoom["level"] = level
        overlay.set_size(w, h, fit_to_screen=False)
        place_overlay()

    def move_to_cursor() -> None:
        if follow["locked"]:
            return
        cx, cy = get_cursor_pos()
        follow["center"] = overlay.follow_center(int(cx), int(cy))

    def toggle_follow() -> None:
        follow["locked"] = not follow["locked"]
        if follow["locked"]:
            overlay.redraw(BORDER_COLOR_LOCKED)
            print("  Border parked — pointer is free. Ctrl+Shift again to follow.")
        else:
            overlay.redraw(BORDER_COLOR)
            print("  Border following the pointer again.")

    def finish() -> None:
        if stopping["done"]:
            return
        stopping["done"] = True
        saved = None
        elapsed = 0.0
        frames = 0
        if recorder is not None:
            print("  Stopping recorder...")
            elapsed = recorder.elapsed_s()
            frames = recorder.frames_written
            try:
                saved = recorder.stop()
            except RuntimeError as exc:
                print(f"  Recorder error: {exc}")
        overlay.destroy()
        if saved:
            mins, secs = divmod(int(elapsed), 60)
            print()
            print(f"  Saved: {saved}")
            print(f"  Length: {mins:02d}:{secs:02d}  ({frames} frames)")
        elif record:
            print("  No video file was written.")

    def tick() -> None:
        if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
            finish()
            return
        ctrl_down = bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
        shift_down = bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
        caps_down = bool(user32.GetAsyncKeyState(VK_CAPITAL) & 0x8000)
        plus_down = bool(
            (user32.GetAsyncKeyState(VK_OEM_PLUS) & 0x8000)
            or (user32.GetAsyncKeyState(VK_ADD) & 0x8000)
        )
        minus_down = bool(
            (user32.GetAsyncKeyState(VK_OEM_MINUS) & 0x8000)
            or (user32.GetAsyncKeyState(VK_SUBTRACT) & 0x8000)
        )
        combo = ctrl_down and shift_down
        if combo and not follow["combo_down"]:
            toggle_follow()
        follow["combo_down"] = combo

        zoom_mods = ctrl_down and caps_down and not shift_down
        now = time.perf_counter()
        if zoom_mods and plus_down:
            if (not zoom["plus"]) or (now - zoom["last"] >= ZOOM_REPEAT_S):
                apply_zoom(ZOOM_STEP)
                zoom["last"] = now
        elif zoom_mods and minus_down:
            if (not zoom["minus"]) or (now - zoom["last"] >= ZOOM_REPEAT_S):
                apply_zoom(1.0 / ZOOM_STEP)
                zoom["last"] = now
        zoom["plus"] = plus_down
        zoom["minus"] = minus_down

        move_to_cursor()
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
        print(f"  Recording  {ratio}   {box_w}x{box_h}   {q['label']} @ {q['fps']} fps")
        if mic_name:
            print(f"  Microphone: {mic_name}")
        else:
            print("  Microphone: off")
        out = default_output_path(ratio, quality, box_w, box_h)
        print(f"  File: {out}")
        print("  Cyan border = captured area (cursor stays in the center).")
        print("  Press Ctrl+Shift to park. Ctrl+Shift again to follow.")
        print("  Hold Ctrl+Caps Lock and + to zoom in, - to zoom out (ratio stays locked).")
        print("  Press Esc to stop and save.")
        recorder = ScreenRecorder(
            width=box_w,
            height=box_h,
            fps=int(q["fps"]),
            crf=int(q["crf"]),
            x264_preset=str(q["preset"]),
            mic_name=mic_name,
            audio_bitrate=str(q["audio_bitrate"]),
            output_path=out,
            get_frame=get_frame,
        )
        assert ffmpeg is not None
        try:
            recorder.start(ffmpeg)
        except Exception as exc:  # noqa: BLE001
            print(f"  Could not start recorder: {exc}")
            overlay.destroy()
            return
    else:
        print(f"  Overlay only: {ratio}   {box_w}x{box_h}")
        print("  Press Ctrl+Shift to park. Ctrl+Caps Lock and + / - to zoom.")
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
            mic_name=session["mic_name"],
            ffmpeg=ffmpeg,
        )
    except KeyboardInterrupt:
        print("\n  Exiting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
